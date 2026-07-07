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
  POST /order  {code, side: buy|sell, qty, price(0=시장가)}
  GET  /fill/<order_no>        누적 체결수량
  POST /cancel {order_no, code, qty}

주식선물 주문: SendOrderFO
  ordKind 1=신규 3=취소 / slbyTp "1"매도 "2"매수 / ordTp "1"지정가 "3"시장가
"""
import logging
import os
import queue
import sys
import threading

from flask import Flask, jsonify, request
from PyQt5.QtWidgets import QApplication
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QTimer, QEventLoop

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kiwoom")

TOKEN = os.environ.get("KIWOOM_GW_TOKEN", "")
PORT = int(os.environ.get("KIWOOM_GW_PORT", "8899"))
SCREEN = "9000"


class KiwoomDaemon:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self.ocx.OnEventConnect.connect(self._on_connect)
        self.ocx.OnReceiveChejanData.connect(self._on_chejan)
        self.ocx.OnReceiveMsg.connect(self._on_msg)
        self.connected = False
        self.account = ""
        self.fills = {}        # order_no -> 누적 체결수량
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
        if order_no:
            self.last_order_no = order_no
            if filled:
                try:
                    self.fills[order_no] = int(filled)
                except ValueError:
                    pass
        log.info("체잔: 주문=%s 상태=%s 체결누계=%s", order_no, status, filled)

    def _on_msg(self, scr, rq, tr, msg):
        self.last_msg = msg
        log.info("MSG [%s] %s", rq, msg)

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


@flask_app.route("/health")
def health():
    if not _auth():
        return jsonify(ok=False, error="unauthorized"), 401
    return jsonify(ok=True, connected=daemon.connected, account=daemon.account)


@flask_app.route("/order", methods=["POST"])
def order():
    if not _auth():
        return jsonify(ok=False, error="unauthorized"), 401
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
        return jsonify(ok=True, filled=daemon.fills[order_no])
    # 데몬 재시작 등으로 추적 이력 없음 → 실패로 응답 (봇이 타임아웃 처리)
    return jsonify(ok=False, error="unknown order_no")


@flask_app.route("/cancel", methods=["POST"])
def cancel():
    if not _auth():
        return jsonify(ok=False, error="unauthorized"), 401
    d = request.get_json(force=True)
    r = daemon.call(daemon._cancel, d["order_no"], d["code"], int(d["qty"]))
    if "error" in r and "value" not in r:
        return jsonify(ok=False, error=r["error"])
    return jsonify(**r["value"])


def main():
    threading.Thread(
        target=lambda: flask_app.run(host="127.0.0.1", port=PORT, threaded=True),
        daemon=True).start()
    daemon.login()
    log.info("kiwoom-daemon 시작: http://127.0.0.1:%d (로그인 창 확인)", PORT)
    sys.exit(daemon.app.exec_())


if __name__ == "__main__":
    main()
