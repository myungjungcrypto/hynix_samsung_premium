#!/usr/bin/env python3
"""키움 OpenAPI+ 주문 데몬 — autotrader의 선물 주문 게이트웨이 (Windows 전용).

반드시 32bit Python으로 실행 (키움 OCX가 32bit COM).
    pip install PyQt5 flask
    python kiwoom_daemon.py

구조:
  - Qt 메인스레드가 키움 OCX 소유 (COM은 생성 스레드에서만 호출 가능)
  - Flask는 백그라운드 스레드에서 127.0.0.1:8899 수신
  - HTTP 요청 → 작업큐 → QTimer가 Qt스레드에서 처리 → Event로 응답 반환
  - 체결은 OnReceiveChejanData 이벤트로 추적 (주문번호별 누적 체결수량)

엔드포인트 (X-Token 헤더 = 환경변수 KIWOOM_GW_TOKEN, 미설정 시 검사 생략):
  GET  /health                 로그인/계좌 상태
  GET  /balance                선물옵션 계좌 잔고 (예탁총액/증거금/포지션)
  POST /order  {code, side: buy|sell, qty, price(0=시장가)}
  GET  /fill/<order_no>        누적 체결수량
  POST /cancel {order_no, code, qty}

주문 잠금: /order·/cancel 은 KIWOOM_GW_ENABLE_ORDERS=1 일 때만 활성화되고,
그 경우에도 localhost 요청만 허용한다. 잔고 조회 전용으로 쓸 때(기본값)는
주문 경로가 403으로 차단되므로 다른 서버에 8899를 열어도 조회만 가능하다.
원격 조회용 바인딩은 KIWOOM_GW_HOST=0.0.0.0 (보안그룹으로 소스 제한 필수).

주식선물 주문: SendOrderFO
  ordKind 1=신규 3=취소 / slbyTp "1"매도 "2"매수 / ordTp "1"지정가 "3"시장가
"""
import logging
import os
from datetime import datetime
import queue
import sys
import threading

from flask import Flask, jsonify, request
from PyQt5.QtWidgets import QApplication
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QTimer, QEventLoop

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout),
                              logging.FileHandler(os.path.join(_LOG_DIR, "daemon.log"),
                                                  encoding="utf-8")])
log = logging.getLogger("kiwoom")


def _load_dotenv():
    """레포 루트의 .env 로드 (시스템 환경변수가 우선)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

TOKEN = os.environ.get("KIWOOM_GW_TOKEN", "")
PORT = int(os.environ.get("KIWOOM_GW_PORT", "8899"))
HOST = os.environ.get("KIWOOM_GW_HOST", "127.0.0.1")
# 주문/취소 활성화 스위치 — 잔고 조회 전용 운용 시 0 유지 (기본)
ENABLE_ORDERS = os.environ.get("KIWOOM_GW_ENABLE_ORDERS", "0") == "1"
SCREEN = "9000"


class KiwoomDaemon:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self.ocx.OnEventConnect.connect(self._on_connect)
        self.ocx.OnReceiveChejanData.connect(self._on_chejan)
        self.ocx.OnReceiveMsg.connect(self._on_msg)
        self.ocx.OnReceiveTrData.connect(self._on_tr)
        self._tr_loop = None
        self.connected = False
        self.account = ""
        self.fills = {}        # order_no -> {"qty": 누적수량, "value": 누적금액, "avg_px": 평균단가}
        self.last_order_no = None
        self.last_msg = ""
        self.jobs = queue.Queue()
        self.timer = QTimer()
        self.timer.timeout.connect(self._drain_jobs)
        self.timer.start(50)   # 50ms마다 작업큐 처리

    # ---------------- 키움 이벤트 ----------------
    def _on_connect(self, err):
        self.connected = (err == 0)
        if self.connected:
            accs = self.ocx.dynamicCall("GetLoginInfo(QString)", "ACCNO").rstrip(";")
            # 선물옵션 계좌는 보통 끝자리 -31. 여러 개면 환경변수로 지정.
            want = os.environ.get("KIWOOM_ACCOUNT", "")
            accounts = accs.split(";")
            self.account = want if want in accounts else accounts[0]
            log.info("로그인 성공. 계좌: %s (전체: %s)", self.account, accs)
        else:
            log.error("로그인 실패 err=%s", err)

    def _on_chejan(self, gubun, item_cnt, fid_list):
        # gubun '0' = 주문접수/체결
        if gubun != "0":
            return
        get = lambda fid: self.ocx.dynamicCall("GetChejanData(int)", fid).strip()
        order_no = get(9203)
        filled = get(911)      # 체결누계수량
        status = get(913)      # 주문상태 (접수/체결/확인)
        px = get(910)          # 체결가 (이번 체결 단가)
        if order_no:
            self.last_order_no = order_no
            if "거부" in status:
                self.fills.setdefault(order_no, {"qty": 0, "value": 0.0, "avg_px": 0.0})["rejected"] = True
            if filled:
                try:
                    new_cum = int(filled)
                    rec = self.fills.setdefault(order_no, {"qty": 0, "value": 0.0, "avg_px": 0.0})
                    delta = new_cum - rec["qty"]
                    if delta > 0 and px:
                        # 수량가중 평균단가 (부호/콤마 방어)
                        unit_px = abs(float(px.replace(",", "").replace("+", "").replace("-", "")))
                        rec["value"] += unit_px * delta
                        rec["qty"] = new_cum
                        rec["avg_px"] = rec["value"] / rec["qty"]
                    elif new_cum > rec["qty"]:
                        rec["qty"] = new_cum
                except ValueError:
                    pass
        log.info("체잔: 주문=%s 상태=%s 체결누계=%s 체결가=%s", order_no, status, filled, px)

    def _on_msg(self, scr, rq, tr, msg):
        self.last_msg = msg
        log.info("MSG [%s] %s", rq, msg)

    def _on_tr(self, scr, rqname, trcode, record, prev_next):
        if self._tr_loop is not None and self._tr_loop.isRunning():
            self._tr_loop.quit()

    # ---------------- TR 조회 (Qt 스레드에서 실행됨) ----------------
    def _tr_request(self, rqname, trcode, inputs, screen):
        for k, v in inputs.items():
            self.ocx.dynamicCall("SetInputValue(QString, QString)", k, v)
        ret = self.ocx.dynamicCall(
            "CommRqData(QString, QString, int, QString)", rqname, trcode, 0, screen)
        if ret != 0:
            raise RuntimeError(f"CommRqData 실패 ret={ret} ({rqname})")
        self._tr_loop = QEventLoop()
        QTimer.singleShot(10000, self._tr_loop.quit)  # 응답 유실 대비 타임아웃
        self._tr_loop.exec_()
        self._tr_loop = None

    def _tr_get(self, trcode, rqname, idx, field):
        return self.ocx.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            trcode, rqname, idx, field).strip()

    @staticmethod
    def _tr_num(s):
        s = (s or "").replace(",", "").replace("+", "").strip()
        if not s:
            return 0
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return 0

    def _futures_balance(self):
        """선물옵션 계좌 잔고 — KOA Studio 실스펙 기준.

        OPW20010(선옵예탁금및증거금조회): TR코드 대문자 필수.
        opw20006(선옵잔고상세현황): 포지션 목록.
        계좌비밀번호는 트레이 AUTO 저장 상태면 빈 문자열로 통과된다.
        """
        acc = self.account
        rq1 = "선옵예탁금"
        self._tr_request(rq1, "OPW20010", {
            "계좌번호": acc,
            "비밀번호": "",
            "비밀번호입력매체구분": "00",
        }, "9101")
        g1 = lambda f: self._tr_get("OPW20010", rq1, 0, f)
        deposit_total = self._tr_num(g1("예탁총액"))
        net_asset = self._tr_num(g1("순자산금액"))
        deposit_cash = self._tr_num(g1("예탁현금"))
        margin = self._tr_num(g1("증거금총액"))
        orderable = self._tr_num(g1("주문가능총액"))
        withdrawable = self._tr_num(g1("인출가능총액"))
        fut_pl = self._tr_num(g1("선물평가손익"))
        opt_pl = self._tr_num(g1("옵션평가손익"))

        rq2 = "선옵잔고"
        positions = []
        try:
            self._tr_request(rq2, "OPW20006", {
                "계좌번호": acc,
                "비밀번호": "",
                "조회일자": datetime.now().strftime("%Y%m%d"),
                "비밀번호입력매체구분": "00",
            }, "9102")
            cnt = self.ocx.dynamicCall(
                "GetRepeatCnt(QString, QString)", "OPW20006", rq2)
            for i in range(cnt):
                g2 = lambda f: self._tr_get("OPW20006", rq2, i, f)
                qty = self._tr_num(g2("잔고수량"))
                if qty == 0:
                    continue
                positions.append({
                    "code": g2("종목코드"),
                    "name": g2("종목명"),
                    "side": g2("매매구분"),
                    "qty": qty,
                    "avg_price": self._tr_num(g2("매입단가")),
                    "current_price": self._tr_num(g2("현재가")),
                    "eval_pl": self._tr_num(g2("평가손익")),
                    "pl_rate": self._tr_num(g2("손익율")),
                    "eval_amount": self._tr_num(g2("평가금액")),
                })
        except Exception as e:
            log.warning("선옵잔고상세(OPW20006) 조회 실패: %s", e)

        # total: 순자산금액(평가손익 반영) 우선, 없으면 예탁총액
        return {
            "total_krw": net_asset or deposit_total,
            "deposit_total_krw": deposit_total,
            "deposit_krw": deposit_cash,
            "margin_krw": margin,
            "orderable_krw": orderable,
            "withdrawable_krw": withdrawable,
            "eval_pl_krw": fut_pl + opt_pl,
            "futures_pl_krw": fut_pl,
            "options_pl_krw": opt_pl,
            "positions": positions,
        }

    def _balance_probe(self):
        """opw20010/opw20007 출력 필드명 탐색 — 값이 비어있지 않은 필드만 반환.

        /balance 가 0만 내놓을 때 실제 필드명을 알아내기 위한 디버그용.
        """
        acc = self.account
        cand10 = [
            "예수금", "예수금액", "인출가능금액", "인출가능총액", "출금가능금액",
            "위탁증거금", "위탁증거금총액", "현금증거금", "유지증거금", "유지증거금총액",
            "평가예탁총액", "예탁총액", "예탁금", "추정예탁자산", "총평가손익",
            "평가손익", "평가손익금액", "매매가능금액", "주문가능금액", "주문가능총액",
            "청산가능금액", "총매입금액", "대용금액", "기본예탁금", "선물정산차금",
            "옵션평가손익", "미수금", "수수료", "결제예정금액", "청산차금",
        ]
        cand07 = [
            "종목코드", "종목명", "매매일자", "매매구분", "잔고수량", "매입단가",
            "매매금액", "현재가", "평가손익", "손익율", "평가금액",
        ]

        rq1 = "선물예수금탐색"
        self._tr_request(rq1, "OPW20010", {
            "계좌번호": acc, "비밀번호": "", "비밀번호입력매체구분": "00",
        }, "9103")
        found10 = {}
        for f in cand10:
            v = self._tr_get("OPW20010", rq1, 0, f)
            if v:
                found10[f] = v

        rq2 = "선물잔고탐색"
        found07 = {}
        cnt = 0
        try:
            self._tr_request(rq2, "OPW20006", {
                "계좌번호": acc, "비밀번호": "",
                "조회일자": datetime.now().strftime("%Y%m%d"),
                "비밀번호입력매체구분": "00",
            }, "9104")
            cnt = self.ocx.dynamicCall(
                "GetRepeatCnt(QString, QString)", "OPW20006", rq2)
            if cnt > 0:
                for f in cand07:
                    v = self._tr_get("OPW20006", rq2, 0, f)
                    if v:
                        found07[f] = v
        except Exception as e:
            found07["_error"] = str(e)

        return {"OPW20010": found10, "opw20006": found07, "opw20006_rows": cnt}

    # ---------------- 작업 처리 (Qt 스레드) ----------------
    def _drain_jobs(self):
        try:
            while True:
                fn, args, done, result = self.jobs.get_nowait()
                try:
                    result["value"] = fn(*args)
                except Exception as e:
                    result["error"] = str(e)
                done.set()
        except queue.Empty:
            pass

    def call(self, fn, *args, timeout=10):
        """Flask 스레드 → Qt 스레드 호출."""
        done = threading.Event()
        result = {}
        self.jobs.put((fn, args, done, result))
        if not done.wait(timeout):
            return {"error": "Qt 스레드 응답 없음"}
        return result

    # ---------------- 주문 함수 (Qt 스레드에서 실행됨) ----------------
    def _send_order(self, code, side, qty, price):
        slby = "2" if side == "buy" else "1"
        ord_tp = "3" if price == 0 else "1"
        price_s = "" if price == 0 else str(price)
        self.last_order_no = None
        ret = self.ocx.dynamicCall(
            "SendOrderFO(QString,QString,QString,QString,int,QString,QString,int,QString,QString)",
            ["arb", SCREEN, self.account, code, 1, slby, ord_tp, int(qty), price_s, ""])
        if ret != 0:
            return {"ok": False, "error": f"SendOrderFO ret={ret} {self.last_msg}"}
        # 주문번호는 체잔 이벤트로 옴 — 최대 3초 대기
        loop = QEventLoop()
        for _ in range(30):
            if self.last_order_no:
                break
            QTimer.singleShot(100, loop.quit)
            loop.exec_()
        if not self.last_order_no:
            return {"ok": False, "error": f"주문번호 미수신 {self.last_msg}"}
        return {"ok": True, "order_no": self.last_order_no}

    def _sfuture_codes(self, base_code):
        """기초자산(예: 000660)의 주식선물 종목코드 목록 — 키움 코드 형식 확인용."""
        out = {}
        # 주식선물 리스트 (월물별 코드)
        try:
            r = self.ocx.dynamicCall("GetSFutureList(QString)", base_code)
            out["GetSFutureList"] = r
        except Exception as e:
            out["GetSFutureList_err"] = str(e)
        try:
            r = self.ocx.dynamicCall("KOA_Functions(QString, QString)",
                                     "GetSFutureList", base_code)
            out["KOA_GetSFutureList"] = r
        except Exception as e:
            out["KOA_err"] = str(e)
        # 종목명 확인용 (코드가 맞으면 이름이 나옴)
        return out

    def _master_name(self, code):
        try:
            return self.ocx.dynamicCall("GetMasterCodeName(QString)", code)
        except Exception as e:
            return f"err: {e}"

    def _cancel(self, order_no, code, qty):
        ret = self.ocx.dynamicCall(
            "SendOrderFO(QString,QString,QString,QString,int,QString,QString,int,QString,QString)",
            ["arb_cxl", SCREEN, self.account, code, 3, "0", "3", int(qty), "", str(order_no)])
        return {"ok": ret == 0, "error": "" if ret == 0 else f"ret={ret} {self.last_msg}"}

    def login(self):
        self.ocx.dynamicCall("CommConnect()")


daemon = KiwoomDaemon()
flask_app = Flask(__name__)


def _auth():
    return not TOKEN or request.headers.get("X-Token", "") == TOKEN


def _order_allowed():
    """주문 경로 이중 잠금: 스위치 켜짐 + localhost 요청만."""
    return ENABLE_ORDERS and request.remote_addr in ("127.0.0.1", "::1")


@flask_app.route("/health")
def health():
    if not _auth():
        return jsonify(ok=False, error="unauthorized"), 401
    return jsonify(ok=True, connected=daemon.connected, account=daemon.account)


@flask_app.route("/balance")
def balance():
    if not _auth():
        return jsonify(ok=False, error="unauthorized"), 401
    if not daemon.connected:
        return jsonify(ok=False, error="키움 미로그인")
    r = daemon.call(daemon._futures_balance, timeout=25)
    if "error" in r and "value" not in r:
        return jsonify(ok=False, error=r["error"])
    return jsonify(ok=True, **r["value"])


@flask_app.route("/balance_debug")
def balance_debug():
    if not _auth():
        return jsonify(ok=False, error="unauthorized"), 401
    if not daemon.connected:
        return jsonify(ok=False, error="키움 미로그인")
    r = daemon.call(daemon._balance_probe, timeout=30)
    if "error" in r and "value" not in r:
        return jsonify(ok=False, error=r["error"])
    return jsonify(ok=True, **r["value"])


@flask_app.route("/order", methods=["POST"])
def order():
    if not _auth():
        return jsonify(ok=False, error="unauthorized"), 401
    if not _order_allowed():
        return jsonify(ok=False, error="orders disabled (set KIWOOM_GW_ENABLE_ORDERS=1, localhost only)"), 403
    if not daemon.connected:
        return jsonify(ok=False, error="키움 미로그인")
    d = request.get_json(force=True)
    r = daemon.call(daemon._send_order, d["code"], d["side"], int(d["qty"]), int(d.get("price", 0)))
    if "error" in r and "value" not in r:
        return jsonify(ok=False, error=r["error"])
    return jsonify(**r["value"])


@flask_app.route("/fill/<order_no>")
def fill(order_no):
    if not _auth():
        return jsonify(ok=False, error="unauthorized"), 401
    if order_no in daemon.fills:
        rec = daemon.fills[order_no]
        return jsonify(ok=True, filled=rec["qty"], avg_price=rec["avg_px"],
                       rejected=rec.get("rejected", False))
    # 데몬 재시작 등으로 추적 이력 없음 → 실패로 응답 (봇이 타임아웃 처리)
    return jsonify(ok=False, error="unknown order_no")


@flask_app.route("/codes/<base_code>")
def codes(base_code):
    """기초자산 코드(000660 등)로 키움 주식선물 종목코드 조회."""
    if not _auth():
        return jsonify(ok=False, error="unauthorized"), 401
    r = daemon.call(daemon._sfuture_codes, base_code)
    return jsonify(ok=True, result=r.get("value", r))


@flask_app.route("/name/<code>")
def name(code):
    """종목코드 → 종목명 (코드 형식 검증용)."""
    if not _auth():
        return jsonify(ok=False, error="unauthorized"), 401
    r = daemon.call(daemon._master_name, code)
    return jsonify(ok=True, name=r.get("value", r))


@flask_app.route("/cancel", methods=["POST"])
def cancel():
    if not _auth():
        return jsonify(ok=False, error="unauthorized"), 401
    if not _order_allowed():
        return jsonify(ok=False, error="orders disabled (set KIWOOM_GW_ENABLE_ORDERS=1, localhost only)"), 403
    d = request.get_json(force=True)
    r = daemon.call(daemon._cancel, d["order_no"], d["code"], int(d["qty"]))
    if "error" in r and "value" not in r:
        return jsonify(ok=False, error=r["error"])
    return jsonify(**r["value"])


def main():
    threading.Thread(
        target=lambda: flask_app.run(host=HOST, port=PORT, threaded=True),
        daemon=True).start()
    daemon.login()
    log.info("kiwoom-daemon 시작: http://%s:%d (주문 %s, 로그인 창 확인)",
             HOST, PORT, "활성" if ENABLE_ORDERS else "차단됨")
    sys.exit(daemon.app.exec_())


if __name__ == "__main__":
    main()
