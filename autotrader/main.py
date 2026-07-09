#!/usr/bin/env python3
"""perp(trade.xyz) vs KRX 주식선물 프리미엄 자동매매 봇 (멀티페어).

mode=monitor : 신호를 텔레그램 알림으로만 (수동 매매)
mode=live    : 자동 주문 — 선물 시장가 매수 → 체결확인 → 체결계약×10주 perp 숏
               청산은 역순. 한쪽 실패 시 즉시 언와인드. 페어별 독립 상태.

프리미엄 (체결가능가·실제환율):
  진입 = perp_bid × USDKRW ÷ 선물_ask − 1 ≥ entry_threshold
  청산 = perp_ask × USDKRW ÷ 선물_bid − 1 ≤ exit_threshold

안전장치:
  - 단일가/이상호가 가드 (역전·스프레드·프리미엄 sanity)
  - autotrader/PAUSE 파일 존재 시 신호 무시 (킬스위치)
  - 페어별 일일 사이클 제한, 주문 중 재신호 차단
  - 기동 시 HL 실포지션 vs state 대조 — 불일치 페어는 거래 비활성
  - 만기 D-N 이내 페어는 live 비활성 (롤 필요)

실행: venv\\Scripts\\python -m autotrader.main
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from autotrader.kis import KISClient, KISQuoteStream, load_keys
from autotrader.hl import HLQuoteStream, HLTrader
from autotrader.broker import make_broker

KST = ZoneInfo("Asia/Seoul")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "state.json")
PAUSE_PATH = os.path.join(BASE_DIR, "PAUSE")
TRADES_PATH = os.path.join(BASE_DIR, "trades.jsonl")
OVERRIDES_PATH = os.path.join(BASE_DIR, "overrides.json")
BASIS_PATH = os.path.join(BASE_DIR, "basis_history.jsonl")


def load_overrides():
    try:
        return json.load(open(OVERRIDES_PATH, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_overrides(ov):
    json.dump(ov, open(OVERRIDES_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def apply_overrides(cfg, ov):
    """텔레그램 set 명령으로 저장된 임계값을 config 위에 덮어씀 (git pull 충돌 회피)."""
    for k, v in ov.get("strategy", {}).items():
        cfg["strategy"][k] = v
    for key, pv in ov.get("pairs", {}).items():
        for p in cfg["pairs"]:
            if p["key"] == key:
                p.update(pv)


def record_trade(rec):
    rec["ts"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with open(TRADES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_trades():
    if not os.path.exists(TRADES_PATH):
        return []
    out = []
    for line in open(TRADES_PATH, encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def trade_pnl(t):
    """기록에서 손익 추출 — 실측(real) 우선, 없으면 추정(est)."""
    if "real_pnl_krw" in t:
        return t["real_pnl_krw"]
    return t.get("est_pnl_krw", 0)


def pnl_report(engine):
    trades = load_trades()
    if not trades:
        lines = ["📊 아직 청산 완료된 사이클이 없습니다."]
    else:
        total = sum(trade_pnl(t) for t in trades)
        wins = sum(1 for t in trades if trade_pnl(t) > 0)
        n_est = sum(1 for t in trades if "real_pnl_krw" not in t or t.get("settle") != "full")
        fut_sum = sum(t.get("fut_pnl_krw", 0) - t.get("kiwoom_fee_krw", 0) for t in trades)
        hl_sum = sum(t.get("hl_closed_pnl_usd", 0) - t.get("hl_fee_usd", 0) for t in trades)
        funding_sum = sum(t.get("funding_usd", 0) for t in trades)
        lines = [
            "📊 누적 성과 (실현 기준)",
            f"청산 사이클: {len(trades)}회 (승 {wins} / 패 {len(trades)-wins}, 승률 {wins/len(trades)*100:.0f}%)",
            f"실현 순익 합계: {total:+,.0f}원"
            + (f" (이 중 {n_est}건 일부추정)" if n_est else ""),
            f"- 선물 레그: {fut_sum:+,.0f}원 (수수료 차감)",
            f"- perp 레그: {hl_sum:+,.2f}$ (수수료 차감)",
            f"- 펀딩 수취: {funding_sum:+,.2f}$",
        ]
        by_key = {}
        for t in trades:
            k = t.get("key", "?")
            by_key.setdefault(k, [0, 0.0])
            by_key[k][0] += 1
            by_key[k][1] += trade_pnl(t)
        for k, (n, s) in by_key.items():
            lines.append(f"- {k}: {n}회, {s:+,.0f}원")
    for p in engine.pairs:
        if p.state.get("position") == "open":
            lines.append(f"보유 중: {p.name} {p.state.get('contracts')}계약 "
                         f"(진입 {p.state.get('entry_prem', 0)*100:+.2f}%, {p.state.get('entry_date','')})")
    return "\n".join(lines)


def status_report(engine):
    lines = [f"🤖 mode={engine.cfg['mode']}"
             + (" ⏸️일시정지" if os.path.exists(PAUSE_PATH) else "")]
    for p in engine.pairs:
        if p.fut_bid > 0 and p.perp_bid > 0 and engine.fx > 0:
            e, x = engine.premiums(p)
            base = engine.baseline(p)
            spread_warn = "" if engine.quotes_sane(p) else " ⚠️이상호가(장외/얇음)"
            if base is None:
                base_line = f"  기준선 수집 중 ({len(p.basis)}샘플)"
            else:
                base_line = (f"  기준선 {base*100:+.2f}% ({len(p.basis)}샘플)"
                             f" | 이격: 진입 {(e-base)*100:+.2f}%p / 청산 {(x-base)*100:+.2f}%p")
            lines.append(f"{p.name} [{p.state['position']}]"
                         + ("" if p.trade_enabled else " [비활성]") + spread_warn
                         + f"\n  선물 {p.fut_bid:,.0f}/{p.fut_ask:,.0f} perp ${p.perp_bid:.2f}"
                         f"\n  프리미엄: 진입 {e*100:+.2f}% / 청산 {x*100:+.2f}% (오늘 {p.cycles['count']}사이클)\n"
                         + base_line)
        else:
            lines.append(f"{p.name} [{p.state['position']}] 시세 대기 중")
    lines.append(f"환율 {engine.fx:,.1f} / 만기 D-{engine.pairs[0].days_to_expiry()}")
    if engine.executor:
        bal = engine.executor.hl.balance()
        if bal:
            lines.append(f"HL 증거금: ${bal[0]:,.0f} (가용 ${bal[1]:,.0f})")
        else:
            lines.append("HL 증거금: 조회 실패")
    return "\n".join(lines)

_LOG_DIR = os.path.join(os.path.dirname(BASE_DIR), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout),
                              logging.FileHandler(os.path.join(_LOG_DIR, "autotrader.log"),
                                                  encoding="utf-8")])
log = logging.getLogger("auto")


def load_dotenv():
    path = os.path.join(os.path.dirname(BASE_DIR), ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Telegram:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    def send(self, text):
        head = text.replace("\n", " / ")[:120]
        if not self.token or not self.chat_id:
            log.warning("[TG미설정] %s", head)
            return
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                              json={"chat_id": self.chat_id, "text": text}, timeout=10)
            if r.ok:
                log.info("[TG전송] %s", head)
            else:
                log.error("[TG거부] %s | %s", r.text[:150], head)
        except Exception as e:
            log.error("[TG오류] %s | %s", e, head)


class PairCtx:
    """페어별 시세·포지션·신호 상태."""

    def __init__(self, pcfg):
        self.cfg = pcfg
        self.key = pcfg["key"]
        self.name = pcfg["name"]
        self.fut_bid = self.fut_ask = 0.0
        self.perp_bid = self.perp_ask = 0.0
        self.fut_ts = self.perp_ts = 0.0
        self.state = {"position": "flat"}
        self.last_alert_ts = 0.0
        self.executing = False
        self.cycles = {"date": "", "count": 0}
        self.trade_enabled = True   # preflight 실패 시 해당 페어만 비활성
        self.basis = []             # [(ts, mid_prem)] 최근 7일 — 베이시스 기준선용
        self.last_basis_ts = 0.0

    def days_to_expiry(self):
        exp = date.fromisoformat(self.cfg["futures_expiry"])
        return (exp - datetime.now(KST).date()).days


class Executor:
    """live 모드 듀얼레그 주문 실행. 진입: 선물 먼저(체결확인) → perp 매칭."""

    def __init__(self, cfg, broker, hl: HLTrader, tg):
        self.cfg = cfg
        self.broker = broker
        self.hl = hl
        self.tg = tg

    def _wait_fill(self, odno, want, timeout=15):
        """(체결수량, 평균단가|None, 거부여부) — timeout 내 폴링, 거부 시 즉시 반환."""
        deadline = time.time() + timeout
        filled, avg_px = 0, None
        while time.time() < deadline:
            q, px, rejected = self.broker.fill_info(odno)
            if rejected:
                return 0, None, True
            if q is not None:
                filled = max(filled, q)
                avg_px = px or avg_px
                if filled >= want:
                    break
            time.sleep(1)
        return filled, avg_px, False

    def enter(self, engine, ctx: PairCtx, prem):
        code = ctx.cfg.get("order_code", ctx.cfg["futures_code"])
        coin = ctx.cfg["perp_coin"]
        mult = ctx.cfg["contract_multiplier"]
        n = ctx.cfg.get("max_contracts", 1)
        self.tg.send(f"⚙️ [진입 실행] {ctx.name} 선물 {code} {n}계약 시장가 매수 (프리미엄 {prem*100:+.2f}%)")

        ok, odno = self.broker.order(code, "buy", n, price=0)
        if not ok:
            self.tg.send(f"❌ {ctx.name} 선물 주문 실패: {odno}\n→ 진입 중단 (flat 유지)")
            return

        filled, fut_px, rejected = self._wait_fill(odno, n)
        if rejected:
            self.tg.send(f"❌ {ctx.name} 선물 주문 거부됨 (증거금/예수금 확인 — 영웅문 주문내역에 사유 표시)\n→ 진입 중단")
            return
        if filled == 0:
            ok_c, msg = self.broker.cancel(odno, code, n)
            self.tg.send(f"❌ {ctx.name} 선물 미체결(15s) → 취소 {'성공' if ok_c else '실패:'+msg}\n→ 진입 중단")
            return
        if filled < n:
            self.broker.cancel(odno, code, n - filled)

        size = filled * mult
        try:
            self.hl.ensure_leverage(coin)
            ok2, res = self.hl.market_order(coin, is_buy=False, size=size)
        except Exception as e:
            ok2, res = False, f"예외 {e}"
        if not ok2:
            # 어떤 이유로든 perp 숏이 안 되면 즉시 선물 언와인드 (헤지 없는 방향 노출 방지)
            ok3, od2 = self.broker.order(code, "sell", filled, price=0)
            self.tg.send(f"🚨 {ctx.name} perp 숏 실패({res})\n→ 선물 언와인드 {'주문완료' if ok3 else '❌실패 — 수동 개입 필요!'}")
            return

        ctx.state = {"position": "open", "contracts": filled, "perp_size": float(res["size"]),
                     "entry_prem": prem, "entry_ts": time.time(),
                     "entry_fut_price": fut_px or ctx.fut_ask,
                     "entry_fut_actual": bool(fut_px),
                     "entry_hl_oid": res.get("oid"), "entry_hl_px": res.get("avg_px"),
                     "entry_date": datetime.now(KST).strftime("%Y-%m-%d")}
        engine.save_state()
        self.tg.send(f"✅ [진입 완료] {ctx.name} 선물 {filled}계약 @{(fut_px or ctx.fut_ask):,.0f}"
                     f" + perp 숏 {res['size']}주 @${res.get('avg_px', 0):,.2f}\n"
                     f"진입 프리미엄 {prem*100:+.2f}%")

    def exit(self, engine, ctx: PairCtx, prem):
        code = ctx.cfg.get("order_code", ctx.cfg["futures_code"])
        coin = ctx.cfg["perp_coin"]
        mult = ctx.cfg["contract_multiplier"]
        n = int(ctx.state.get("contracts", 0))
        perp_size = float(ctx.state.get("perp_size", n * mult))
        if n <= 0:
            ctx.state = {"position": "flat"}
            engine.save_state()
            return
        self.tg.send(f"⚙️ [청산 실행] {ctx.name} 선물 {n}계약 매도 + perp 커버 (프리미엄 {prem*100:+.2f}%)")

        ok, odno = self.broker.order(code, "sell", n, price=0)
        if not ok:
            self.tg.send(f"❌ {ctx.name} 선물 매도 실패: {odno}\n→ 청산 중단, 다음 신호에 재시도")
            return
        filled, exit_fut_px, rejected = self._wait_fill(odno, n)
        if rejected:
            self.tg.send(f"❌ {ctx.name} 선물 매도 거부됨 — 수동 확인 필요 (포지션 유지 중)")
            return
        if filled == 0:
            self.broker.cancel(odno, code, n)
            self.tg.send(f"❌ {ctx.name} 선물 매도 미체결 → 취소, 다음 신호에 재시도")
            return

        cover = perp_size * (filled / n)
        try:
            ok2, res = self.hl.market_close(coin, cover)
        except Exception as e:
            ok2, res = False, f"예외 {e}"
        if not ok2:
            self.tg.send(f"🚨 {ctx.name} perp 커버 실패({res}) — perp 숏 {cover}주 잔존, 수동 확인 필요!")
            res = {}

        rec, msg = self._settle(engine, ctx, prem, filled, exit_fut_px, res, mult)
        record_trade(rec)

        if filled >= n:
            ctx.state = {"position": "flat"}
            engine.save_state()
            self.tg.send(f"✅ [청산 완료] {ctx.name}\n{msg}")
        else:
            ctx.state["contracts"] = n - filled
            ctx.state["perp_size"] = perp_size - cover
            engine.save_state()
            self.tg.send(f"⚠️ {ctx.name} 부분 청산 {filled}/{n}\n{msg}\n잔여 {n-filled}계약 유지")

    def _settle(self, engine, ctx, prem, filled, exit_fut_px, hl_res, mult):
        """실측 정산: 선물 레그(체결가·수수료) + HL 레그(실현손익·수수료·펀딩)."""
        st = ctx.state
        entry_prem = st.get("entry_prem", 0)
        captured = entry_prem - prem
        fee_rate = self.cfg.get("fees", {}).get("kiwoom_fee_rate", 0.00003)

        entry_px = st.get("entry_fut_price", ctx.fut_bid)
        exit_px = exit_fut_px or ctx.fut_bid
        actual_px = st.get("entry_fut_actual", False) and bool(exit_fut_px)
        fut_pnl = (exit_px - entry_px) * mult * filled
        kiwoom_fee = (entry_px + exit_px) * mult * filled * fee_rate

        settle = "full"
        hl_detail = self.hl.fills_for_oids([st.get("entry_hl_oid"), hl_res.get("oid")])
        funding = self.hl.funding_between(ctx.cfg["perp_coin"],
                                          st.get("entry_ts", time.time()) * 1000,
                                          time.time() * 1000)
        if hl_detail is None:
            # 폴백: 체결 평균가 기반 (수수료 미반영)
            e_px, x_px = st.get("entry_hl_px") or 0, hl_res.get("avg_px") or 0
            hl_detail = {"closed_pnl": (e_px - x_px) * float(hl_res.get("size", 0)), "fee": 0.0}
            settle = "partial"
        if funding is None:
            funding = 0.0
            settle = "partial"
        if not actual_px:
            settle = "partial"

        hl_net_usd = hl_detail["closed_pnl"] - hl_detail["fee"] + funding
        fx = engine.fx or 0
        real_pnl = fut_pnl - kiwoom_fee + hl_net_usd * fx

        rec = {"key": ctx.key, "contracts": filled, "settle": settle,
               "entry_prem": round(entry_prem, 5), "exit_prem": round(prem, 5),
               "captured_pct": round(captured * 100, 3),
               "fut_entry_px": entry_px, "fut_exit_px": exit_px,
               "fut_pnl_krw": round(fut_pnl), "kiwoom_fee_krw": round(kiwoom_fee),
               "hl_closed_pnl_usd": round(hl_detail["closed_pnl"], 2),
               "hl_fee_usd": round(hl_detail["fee"], 4),
               "funding_usd": round(funding, 4),
               "fx": round(fx, 1),
               "real_pnl_krw": round(real_pnl),
               "entry_date": st.get("entry_date", "")}
        msg = (f"실현손익 {real_pnl:+,.0f}원 ({'실측' if settle=='full' else '일부추정'})\n"
               f"- 선물 {fut_pnl:+,.0f}원 (수수료 -{kiwoom_fee:,.0f}원)\n"
               f"- perp {hl_detail['closed_pnl']:+,.2f}$ (수수료 -{hl_detail['fee']:.2f}$)\n"
               f"- 펀딩 {funding:+,.2f}$ | 캡처 {captured*100:+.2f}%p")
        return rec, msg


class Engine:
    def __init__(self, cfg, tg):
        self.cfg = cfg
        self.tg = tg
        self.executor = None
        self.fx = 0.0
        self.pairs = [PairCtx(p) for p in cfg["pairs"]]
        self.by_fut = {p.cfg["futures_code"]: p for p in self.pairs}
        self.by_coin = {p.cfg["perp_coin"]: p for p in self.pairs}
        self.last_status_ts = 0.0
        self.paused_notified = False
        self._load_state()
        self._load_basis()

    # ---------------- 베이시스 기준선 ----------------
    # 진입/청산은 절대 프리미엄이 아니라 "기준선(최근 N일 평균 스프레드) 대비 이격"으로 판정.
    # 데이터가 N일 미만이면 쌓인 만큼의 확장 평균 사용 (오늘→1일→...→7일 롤링).
    def _basis_window_sec(self):
        return self.cfg.get("strategy", {}).get("basis_window_days", 7) * 86400

    def _load_basis(self):
        if not os.path.exists(BASIS_PATH):
            return
        cutoff = time.time() - self._basis_window_sec()
        by_key = {p.key: p for p in self.pairs}
        for line in open(BASIS_PATH, encoding="utf-8"):
            try:
                r = json.loads(line)
                if r["ts"] >= cutoff and r["key"] in by_key:
                    by_key[r["key"]].basis.append((r["ts"], r["prem"]))
            except (json.JSONDecodeError, KeyError):
                pass
        for p in self.pairs:
            log.info("%s 베이시스 샘플 %d개 로드", p.key, len(p.basis))

    def record_basis(self, p: PairCtx):
        """60초마다 mid 기준 스프레드 샘플 적재 (판정과 무관하게 데이터 수집)."""
        now = time.time()
        if now - p.last_basis_ts < 60:
            return
        p.last_basis_ts = now
        fut_mid = (p.fut_bid + p.fut_ask) / 2
        perp_mid = (p.perp_bid + p.perp_ask) / 2
        prem = perp_mid * self.fx / fut_mid - 1
        p.basis.append((now, prem))
        cutoff = now - self._basis_window_sec()
        while p.basis and p.basis[0][0] < cutoff:
            p.basis.pop(0)
        try:
            with open(BASIS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": round(now, 1), "key": p.key,
                                    "prem": round(prem, 6)}) + "\n")
        except OSError as e:
            log.warning("베이시스 기록 실패: %s", e)

    def baseline(self, p: PairCtx):
        """기준선 = 윈도 내 평균 스프레드. 샘플 부족 시 None (진입 보류)."""
        min_n = self.cfg.get("strategy", {}).get("basis_min_samples", 10)
        if len(p.basis) < min_n:
            return None
        return sum(v for _, v in p.basis) / len(p.basis)

    # ---------------- state ----------------
    def _load_state(self):
        try:
            data = json.load(open(STATE_PATH, encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        for p in self.pairs:
            st = data.get(p.key)
            if isinstance(st, dict) and "position" in st:
                p.state = st

    def save_state(self):
        data = {p.key: p.state for p in self.pairs}
        json.dump(data, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ---------------- 콜백 ----------------
    def on_fut(self, code, bid, ask):
        p = self.by_fut.get(code)
        if p:
            p.fut_bid, p.fut_ask, p.fut_ts = bid, ask, time.time()
            self.evaluate(p)

    def on_perp(self, coin, bid, ask):
        p = self.by_coin.get(coin)
        if p:
            p.perp_bid, p.perp_ask, p.perp_ts = bid, ask, time.time()
            self.evaluate(p)

    # ---------------- 게이트 ----------------
    def in_session(self):
        now = datetime.now(KST)
        if now.weekday() >= 5:
            return False
        s = dtime.fromisoformat(self.cfg["session"]["start"])
        e = dtime.fromisoformat(self.cfg["session"]["end"])
        return s <= now.time() <= e

    def fresh(self, p: PairCtx):
        now = time.time()
        return (self.fx > 0 and p.fut_bid > 0 and p.perp_bid > 0
                and now - p.fut_ts < 30 and now - p.perp_ts < 30)

    def quotes_sane(self, p: PairCtx):
        strat = self.cfg["strategy"]
        if p.fut_ask <= p.fut_bid or p.perp_ask <= p.perp_bid:
            return False
        fut_mid = (p.fut_ask + p.fut_bid) / 2
        if (p.fut_ask - p.fut_bid) / fut_mid > strat.get("max_fut_spread", 0.003):
            return False
        return True

    def paused(self):
        if os.path.exists(PAUSE_PATH):
            if not self.paused_notified:
                self.paused_notified = True
                self.tg.send("⏸️ PAUSE 파일 감지 — 신호 무시 중 (해제: PAUSE 파일 삭제)")
            return True
        self.paused_notified = False
        return False

    def _cycle_ok(self, p: PairCtx):
        today = datetime.now(KST).strftime("%Y-%m-%d")
        if p.cycles["date"] != today:
            p.cycles = {"date": today, "count": 0}
        limit = self.cfg.get("live", {}).get("max_cycles_per_day", 3)
        return p.cycles["count"] < limit

    def _register_cycle(self, p: PairCtx):
        today = datetime.now(KST).strftime("%Y-%m-%d")
        if p.cycles["date"] != today:
            p.cycles = {"date": today, "count": 0}
        p.cycles["count"] += 1

    # ---------------- 핵심 ----------------
    def premiums(self, p: PairCtx):
        entry = p.perp_bid * self.fx / p.fut_ask - 1
        exit_ = p.perp_ask * self.fx / p.fut_bid - 1
        return entry, exit_

    def thresholds(self, p: PairCtx):
        strat = self.cfg["strategy"]
        return (p.cfg.get("entry_threshold", strat["entry_threshold"]),
                p.cfg.get("exit_threshold", strat["exit_threshold"]))

    def evaluate(self, p: PairCtx):
        if p.executing or not (self.fresh(p) and self.in_session() and self.quotes_sane(p)):
            return
        entry, exit_ = self.premiums(p)
        strat = self.cfg["strategy"]
        now = time.time()
        cooldown = strat.get("alert_cooldown_sec", 600)

        if abs(entry) > strat.get("premium_sanity", 0.05):
            if now - p.last_alert_ts >= cooldown:
                p.last_alert_ts = now
                log.warning("%s 프리미엄 sanity 초과 %+.2f%% — 스킵", p.key, entry * 100)
                self.tg.send(f"⚠️ {p.name} 프리미엄 {entry*100:+.1f}% — 비정상 호가로 판단, 신호 무시\n"
                             f"선물 {p.fut_bid:,.0f}/{p.fut_ask:,.0f} perp {p.perp_bid:.2f}/{p.perp_ask:.2f} fx {self.fx:,.1f}")
            return

        self._status_log()

        self.record_basis(p)
        if self.paused() or not p.trade_enabled:
            return
        base = self.baseline(p)
        if base is None:
            return  # 기준선 샘플 수집 중 — 진입 보류

        entry_th, exit_th = self.thresholds(p)
        if p.state["position"] == "flat" and (entry - base) >= entry_th:
            if now - p.last_alert_ts >= cooldown and self._cycle_ok(p):
                p.last_alert_ts = now
                self._dispatch(p, "enter", entry, base)
        elif p.state["position"] == "open" and (exit_ - base) <= exit_th:
            if now - p.last_alert_ts >= cooldown:
                p.last_alert_ts = now
                self._dispatch(p, "exit", exit_, base)

    def _status_log(self):
        now = time.time()
        if now - self.last_status_ts < self.cfg.get("status_log_sec", 60):
            return
        self.last_status_ts = now
        for p in self.pairs:
            if p.fut_bid > 0 and p.perp_bid > 0 and self.fx > 0:
                e, x = self.premiums(p)
                log.info("%s 선물 %.0f/%.0f perp %.2f/%.2f | 진입 %+.3f%% 청산 %+.3f%% [%s]",
                         p.key, p.fut_bid, p.fut_ask, p.perp_bid, p.perp_ask,
                         e * 100, x * 100, p.state["position"])

    def _dispatch(self, p: PairCtx, action, prem, base=0.0):
        gap = (prem - base) * 100
        detail = f"이격 {gap:+.2f}%p (프리미엄 {prem*100:+.2f}%, 기준선 {base*100:+.2f}%)"
        if self.cfg["mode"] == "monitor":
            d2e = p.days_to_expiry()
            if action == "enter":
                self.tg.send(f"🚨 [진입신호/모니터] {p.name}\n{detail}\n"
                             f"선물 ask {p.fut_ask:,.0f} / perp bid ${p.perp_bid:.2f} (fx {self.fx:,.1f})\n"
                             f"→ 수동: 선물 {p.cfg['futures_code']} 매수 + perp 숏"
                             + (f"\n⚠️ 만기 D-{d2e}" if d2e <= 3 else ""))
                p.state["position"] = "open"
            else:
                self.tg.send(f"✅ [청산신호/모니터] {p.name}\n{detail}\n"
                             f"→ 수동: 선물 매도 + perp 숏 커버")
                p.state["position"] = "flat"
            self.save_state()
            return

        p.executing = True
        if action == "enter":
            self._register_cycle(p)

        async def run():
            try:
                fn = self.executor.enter if action == "enter" else self.executor.exit
                await asyncio.to_thread(fn, self, p, prem)
            except Exception as e:
                log.exception("%s 실행기 오류", p.key)
                self.tg.send(f"🚨 {p.name} 실행기 예외: {e} — 포지션 수동 확인 필요")
            finally:
                p.executing = False

        asyncio.get_running_loop().create_task(run())


def handle_set(engine, cmd):
    """`set entry 0.7 [KEY]` / `set exit 0.1 [KEY]` — 임계값 변경 (% 단위 입력, 재시작 후에도 유지).

    KEY 생략 시 전체 공통값 변경, KEY(skhx/smsn) 지정 시 해당 종목만.
    """
    parts = cmd.split()
    strat = engine.cfg["strategy"]

    def current():
        lines = [f"현재 설정: 진입 +{strat['entry_threshold']*100:.2f}% / 청산 {strat['exit_threshold']*100:.2f}% (공통)"]
        for p in engine.pairs:
            e_th, x_th = engine.thresholds(p)
            marks = []
            if "entry_threshold" in p.cfg:
                marks.append("진입 개별")
            if "exit_threshold" in p.cfg:
                marks.append("청산 개별")
            lines.append(f"- {p.name}: 진입 +{e_th*100:.2f}% / 청산 {x_th*100:.2f}%"
                         + (f" ({', '.join(marks)})" if marks else ""))
        return "\n".join(lines)

    if len(parts) < 3:
        return current()

    field = {"entry": "entry_threshold", "exit": "exit_threshold"}.get(parts[1])
    if not field:
        return "형식: set entry 0.7 [skhx]  /  set exit 0.1 [smsn]"
    try:
        pct = float(parts[2])
    except ValueError:
        return f"숫자가 아님: {parts[2]} (예: set entry 0.7 = 0.7%)"
    if field == "entry_threshold" and not (0.05 <= pct <= 5):
        return f"진입 임계값은 0.05~5(%) 범위로 (입력: {pct})"
    if field == "exit_threshold" and not (-1 <= pct <= 2):
        return f"청산 임계값은 -1~2(%) 범위로 (입력: {pct})"
    val = pct / 100

    ov = load_overrides()
    key = parts[3].upper() if len(parts) > 3 else None
    if key:
        pair = next((p for p in engine.pairs if p.key == key), None)
        if not pair:
            return f"모르는 종목: {key} (가능: " + ", ".join(p.key for p in engine.pairs) + ")"
        pair.cfg[field] = val
        ov.setdefault("pairs", {}).setdefault(key, {})[field] = val
    else:
        strat[field] = val
        ov.setdefault("strategy", {})[field] = val
    save_overrides(ov)
    log.info("임계값 변경: %s=%.4f%% (%s)", field, pct, key or "공통")
    return "✅ 변경 완료\n" + current()


async def tg_commands(engine, tg):
    """텔레그램 명령 수신 (전용 봇 토큰 필요 — 알림봇과 getUpdates 충돌 방지).

    .env에 TELEGRAM_BOT_TOKEN_AT=<새 봇 토큰> 설정 시 활성화.
    명령: pnl(성과) / status(현황) / pause(일시정지) / resume(재개)
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN_AT", "")
    if not token:
        log.info("TELEGRAM_BOT_TOKEN_AT 미설정 — 텔레그램 명령 비활성 (알림은 기존대로 발송)")
        return
    chat_id = tg.chat_id
    api = f"https://api.telegram.org/bot{token}"

    def reply(text):
        try:
            requests.post(f"{api}/sendMessage",
                          json={"chat_id": chat_id, "text": text}, timeout=10)
        except Exception as e:
            log.error("명령 응답 실패: %s", e)

    offset = 0
    log.info("텔레그램 명령 활성 (pnl / status / pause / resume)")
    while True:
        try:
            r = await asyncio.to_thread(
                requests.get, f"{api}/getUpdates",
                params={"offset": offset + 1, "timeout": 25}, timeout=35)
            for u in r.json().get("result", []):
                offset = max(offset, u["update_id"])
                msg = u.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != str(chat_id):
                    continue
                cmd = (msg.get("text") or "").strip().lower().lstrip("/")
                if cmd in ("pnl", "수익", "성과"):
                    reply(pnl_report(engine))
                elif cmd in ("status", "상태"):
                    reply(status_report(engine))
                elif cmd == "pause":
                    open(PAUSE_PATH, "w").close()
                    reply("⏸️ 일시정지 — 신규 신호 무시 (재개: resume)")
                elif cmd == "resume":
                    if os.path.exists(PAUSE_PATH):
                        os.remove(PAUSE_PATH)
                    reply("▶️ 재개 — 신호 감시 중")
                elif cmd.startswith("set") or cmd == "설정":
                    reply(handle_set(engine, cmd))
                elif cmd:
                    reply("명령: pnl(성과) / status(현황) / pause / resume\n"
                          "set entry 0.7 — 진입 임계값 0.7%로\n"
                          "set exit 0.1 [skhx] — 청산 임계값 (종목 지정 가능)\n"
                          "set — 현재 설정 보기")
        except Exception as e:
            log.warning("텔레그램 명령 폴링 오류: %s", e)
            await asyncio.sleep(10)


async def fx_loop(engine, interval):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?range=1d&interval=1m"
    naver = ("https://m.stock.naver.com/front-api/marketIndex/productDetail"
             "?category=exchange&reutersCode=FX_USDKRW")
    headers = {"User-Agent": "Mozilla/5.0"}
    while True:
        try:
            px = requests.get(url, headers=headers, timeout=10).json()[
                "chart"]["result"][0]["meta"]["regularMarketPrice"]
            engine.fx = float(px)
        except Exception:
            try:
                d = requests.get(naver, headers=headers, timeout=10).json()
                engine.fx = float(d["result"]["closePrice"].replace(",", ""))
            except Exception as e:
                log.warning("FX 갱신 실패: %s", e)
        await asyncio.sleep(interval)


async def watchdog(engine, kis, tg):
    """호가 정지 시 REST 재확인, 그래도 실패면 경고."""
    warned = set()
    while True:
        await asyncio.sleep(30)
        if not engine.in_session():
            warned.clear()
            continue
        problems = []
        for p in engine.pairs:
            if time.time() - p.fut_ts > 60:
                try:
                    b, a, _, _ = await asyncio.to_thread(kis.asking_price, p.cfg["futures_code"])
                    p.fut_bid, p.fut_ask, p.fut_ts = b, a, time.time()
                    log.info("%s 선물 호가 REST 갱신: %.0f/%.0f", p.key, b, a)
                except Exception as e:
                    problems.append(f"{p.key}선물({e})")
            if time.time() - p.perp_ts > 60:
                try:
                    r = requests.post("https://api.hyperliquid.xyz/info",
                                      json={"type": "l2Book", "coin": p.cfg["perp_coin"]},
                                      timeout=10).json()
                    p.perp_bid = float(r["levels"][0][0]["px"])
                    p.perp_ask = float(r["levels"][1][0]["px"])
                    p.perp_ts = time.time()
                except Exception as e:
                    problems.append(f"{p.key}perp({e})")
        key = ",".join(problems)
        if problems and key not in warned:
            tg.send(f"⚠️ 시세 수신 장애 (REST 재확인도 실패): {key}")
            warned.add(key)
        elif not problems:
            warned.clear()


def live_preflight(cfg, engine, broker, tg):
    """live 사전점검. 공통 실패 → monitor 강등, 페어별 실패 → 해당 페어만 비활성."""
    errors = []
    ok_b, msg_b = broker.health()
    if not ok_b:
        errors.append(f"브로커 점검 실패: {msg_b}")
    wallet = os.environ.get("HL_WALLET_ADDRESS", "")
    pkey = os.environ.get("HL_PRIVATE_KEY", "")
    if not wallet or not pkey:
        errors.append("HL_WALLET_ADDRESS / HL_PRIVATE_KEY 미설정")
    hl = None
    if not errors:
        # trade.xyz 같은 builder dex 코인은 dex 명시 로드 필요 (없으면 주문 시 KeyError)
        dexs = sorted({p.cfg["perp_coin"].split(":")[0]
                       for p in engine.pairs if ":" in p.cfg["perp_coin"]})
        hl = HLTrader(wallet, pkey, cfg["hyperliquid"].get("leverage", 3), perp_dexs=dexs)
        if not hl.exchange:
            errors.append("hyperliquid-python-sdk 미설치")
    if errors:
        tg.send("⛔ live 사전점검 실패 → monitor 모드로 강등\n- " + "\n- ".join(errors))
        cfg["mode"] = "monitor"
        return None

    roll_d = cfg["session"].get("roll_days_before_expiry", 2)
    for p in engine.pairs:
        pair_issues = []
        ok_c, msg_c = hl.can_trade(p.cfg["perp_coin"])
        if not ok_c:
            pair_issues.append(f"HL 주문 불가: {msg_c}")
        pos = hl.position(p.cfg["perp_coin"])
        expected = -(p.state.get("perp_size", 0)) if p.state.get("position") == "open" else 0.0
        if pos is not None and abs(pos - expected) > 0.01:
            pair_issues.append(f"HL 포지션 불일치 (실제 {pos} vs 기대 {expected})")
        if p.days_to_expiry() <= roll_d:
            pair_issues.append(f"만기 D-{p.days_to_expiry()} — 차월물로 롤 필요")
        if pair_issues:
            p.trade_enabled = False
            tg.send(f"⛔ {p.name} 거래 비활성:\n- " + "\n- ".join(pair_issues))
    return hl


async def main():
    load_dotenv()
    cfg = json.load(open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8"))
    ov = load_overrides()
    if ov:
        apply_overrides(cfg, ov)
        log.info("임계값 오버라이드 적용: %s", ov)
    tg = Telegram()
    engine = Engine(cfg, tg)

    key, sec, acct = load_keys(cfg.get("kis_secrets_path", ""))
    kis = KISClient(key, sec, acct)
    kis.token()

    broker = make_broker(cfg, kis)
    if cfg["mode"] == "live":
        hl_trader = live_preflight(cfg, engine, broker, tg)
        if hl_trader:
            engine.executor = Executor(cfg, broker, hl_trader, tg)

    # 기동 시 REST 초기 호가
    for p in engine.pairs:
        try:
            b, a, bq, aq = kis.asking_price(p.cfg["futures_code"])
            p.fut_bid, p.fut_ask, p.fut_ts = b, a, time.time()
            log.info("초기 %s 호가: %.0f/%.0f (잔량 %d/%d)", p.key, b, a, bq, aq)
        except Exception as e:
            log.warning("%s 초기 호가 실패(장외?): %s", p.key, e)

    kis_stream = KISQuoteStream(kis, [p.cfg["futures_code"] for p in engine.pairs], engine.on_fut)
    hl_stream = HLQuoteStream([p.cfg["perp_coin"] for p in engine.pairs], engine.on_perp)

    mode_label = "🔴 LIVE(실거래)" if cfg["mode"] == "live" else "monitor"
    lines = []
    for p in engine.pairs:
        e_th, x_th = engine.thresholds(p)
        flag = "" if p.trade_enabled else " [비활성]"
        lines.append(f"- {p.name} {p.cfg['futures_code']} (D-{p.days_to_expiry()}) vs {p.cfg['perp_coin']}"
                     f" / {p.cfg.get('max_contracts',1)}계약{flag}")
    tg.send(f"🤖 자동매매 봇 시작 (mode={mode_label})\n" + "\n".join(lines) +
            f"\n진입 +{cfg['strategy']['entry_threshold']*100:.1f}% / 청산 {cfg['strategy']['exit_threshold']*100:.1f}%"
            f"\n일시정지: autotrader/PAUSE 파일 생성")

    await asyncio.gather(
        kis_stream.run(),
        hl_stream.run(),
        fx_loop(engine, cfg.get("fx_poll_sec", 30)),
        watchdog(engine, kis, tg),
        tg_commands(engine, tg),
    )


if __name__ == "__main__":
    asyncio.run(main())
