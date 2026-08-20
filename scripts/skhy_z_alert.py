# -*- coding: utf-8 -*-
"""SKHY(ADR perp) vs SKHX(본주 perp) 프리미엄 z-스코어 텔레그램 알림.

프리미엄 정의: mid(xyz:SKHY) / (mid(xyz:SKHX) * 0.1) - 1   (1 ADR = 본주 0.1주)

시그널 (CONFIRM_N회 연속 충족 시 발화):
  🔴 z >= +2        → 프리미엄 숏 진입 (SKHY 숏 / SKHX 롱)
  🟢 z <= -2        → 프리미엄 롱 진입
  ⚪ 숏: z <= -0.5 / 롱: z >= +0.5 → 청산 (평균 지나 오버슈팅까지)
  🛑 z >= +3.5 또는 프리미엄 >= 55% → 하드스톱 (레짐 전환 방어선)

사용법:
  python scripts/skhy_z_alert.py --backfill   # HL 캔들로 과거 21일 적재 (최초 1회)
  python scripts/skhy_z_alert.py --state short # 현재 보유 포지션을 수동 등록
  python scripts/skhy_z_alert.py --once       # 1회 샘플+판정 (cron/schtasks용)
  python scripts/skhy_z_alert.py              # 상주 루프 (5분 주기)
"""
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.path.join(ROOT, "autotrader", "skhy_premium_history.jsonl")
STATE = os.path.join(ROOT, "autotrader", "skhy_z_state.json")
LOGDIR = os.path.join(ROOT, "logs")

API = "https://api.hyperliquid.xyz/info"
DEX = "xyz"
ADR_COIN = "xyz:SKHY"
ORD_COIN = "xyz:SKHX"
ADR_PER_ORD = 0.1          # 1 ADR = 본주 0.1주

INTERVAL_SEC = 300         # 5분 샘플
WINDOW_DAYS = 14           # 롤링 윈도우 (백테스트 2026-08: 21d는 신호 과소, 7d는 과적합 위험 → 14d)
MIN_SAMPLES = 1000         # 이 미만이면 수집만
ENTRY_Z = 2.0
EXIT_Z = -0.5              # 오버슈팅 청산 (백테스트: z=0 청산 대비 SKHY +9%p, TSMC +1~5%p/년 우위)
HARD_Z = 3.5
HARD_PREM = 0.55
CONFIRM_N = 3              # 연속 충족 횟수 (15분)

KST = timezone(timedelta(hours=9))


def log(msg):
    line = f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        with open(os.path.join(LOGDIR, "skhy_z.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_env():
    """레포 .env에서 텔레그램 설정 로드 (환경변수 우선)."""
    cfg = {}
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    cfg[k.strip()] = v.strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("TELEGRAM_CHAT_ID", "")
    return token, chat


def tg_send(text):
    token, chat = load_env()
    if not token or not chat:
        log(f"[TG미설정] {text}")
        return
    try:
        data = json.dumps({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        log(f"[TG전송] {text.splitlines()[0]}")
    except Exception as e:  # noqa: BLE001 — 알림 실패가 루프를 죽이면 안 됨
        log(f"[TG실패] {e}")


def info(payload):
    req = urllib.request.Request(API, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def fetch_premium():
    meta, ctxs = info({"type": "metaAndAssetCtxs", "dex": DEX})
    mids = {}
    for a, c in zip(meta["universe"], ctxs):
        if a["name"] in (ADR_COIN, ORD_COIN) and c.get("midPx"):
            mids[a["name"]] = float(c["midPx"])
    adr, ordi = mids.get(ADR_COIN), mids.get(ORD_COIN)
    if not adr or not ordi:
        raise RuntimeError(f"mid 조회 실패: {mids}")
    return adr, ordi, adr / (ordi * ADR_PER_ORD) - 1


def append_hist(ts, adr, ordi, prem):
    with open(HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "adr": adr, "ord": ordi,
                            "prem": round(prem, 6)}) + "\n")


def load_hist():
    if not os.path.exists(HIST):
        return []
    cutoff = time.time() - WINDOW_DAYS * 86400
    rows = []
    with open(HIST, encoding="utf-8") as f:
        for ln in f:
            try:
                r = json.loads(ln)
                if r["ts"] >= cutoff:
                    rows.append(r)
            except (json.JSONDecodeError, KeyError):
                continue
    return rows


def zscore(rows, prem):
    vals = [r["prem"] for r in rows]
    n = len(vals)
    if n < MIN_SAMPLES:
        return None, None, None
    mu = sum(vals) / n
    var = sum((v - mu) ** 2 for v in vals) / n
    sigma = math.sqrt(var)
    if sigma <= 0:
        return None, mu, sigma
    return (prem - mu) / sigma, mu, sigma


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {"pos": "flat", "streak_sig": "", "streak_n": 0}


def save_state(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


def classify(pos, z, prem):
    """현재 샘플이 만드는 시그널 후보 (없으면 '')."""
    if pos == "short" and (z >= HARD_Z or prem >= HARD_PREM):
        return "hardstop"
    if pos == "long" and z <= -HARD_Z:
        return "hardstop"
    if pos == "flat":
        if z >= ENTRY_Z:
            return "enter_short"
        if z <= -ENTRY_Z:
            return "enter_long"
    elif pos == "short" and z <= EXIT_Z:
        return "exit"
    elif pos == "long" and z >= -EXIT_Z:
        return "exit"
    return ""


def fire(sig, st, z, mu, sigma, prem, adr, ordi):
    head = {
        "enter_short": "🔴 [SKHY z-알림] 프리미엄 숏 진입 시그널 (z ≥ +2)",
        "enter_long": "🟢 [SKHY z-알림] 프리미엄 롱 진입 시그널 (z ≤ -2)",
        "exit": "⚪ [SKHY z-알림] 청산 시그널 (오버슈팅 ∓0.5 도달)",
        "hardstop": "🛑 [SKHY z-알림] 하드스톱! (z ≥ +3.5 또는 프리미엄 ≥ 55%)",
    }[sig]
    tg_send(f"{head}\n"
            f"프리미엄 {prem*100:+.2f}% | z = {z:+.2f}\n"
            f"μ({WINDOW_DAYS}d) = {mu*100:.2f}% | σ = {sigma*100:.2f}%p\n"
            f"SKHY ${adr:,.2f} / SKHX ${ordi:,.2f}\n"
            f"({CONFIRM_N}회 연속 확인 완료)")
    st["pos"] = {"enter_short": "short", "enter_long": "long",
                 "exit": "flat", "hardstop": "flat"}[sig]


def run_once():
    adr, ordi, prem = fetch_premium()
    ts = time.time()
    append_hist(ts, adr, ordi, prem)
    rows = load_hist()
    z, mu, sigma = zscore(rows, prem)
    st = load_state()
    if z is None:
        log(f"수집중 {len(rows)}/{MIN_SAMPLES} | prem {prem*100:+.2f}%")
        return
    sig = classify(st["pos"], z, prem)
    if sig and sig == st.get("streak_sig"):
        st["streak_n"] += 1
    else:
        st["streak_sig"], st["streak_n"] = sig, (1 if sig else 0)
    log(f"prem {prem*100:+.2f}% z {z:+.2f} pos={st['pos']}"
        f" sig={sig or '-'} streak={st['streak_n']}")
    if sig and st["streak_n"] >= CONFIRM_N:
        fire(sig, st, z, mu, sigma, prem, adr, ordi)
        st["streak_sig"], st["streak_n"] = "", 0
    save_state(st)


def backfill():
    """HL 5분 캔들로 과거 WINDOW_DAYS치 프리미엄 적재 (기존 파일 대체)."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=WINDOW_DAYS)

    def candles(coin):
        out, t = {}, start
        while t < now:
            t2 = min(t + timedelta(days=14), now)  # 5000봉 캡 회피
            cs = info({"type": "candleSnapshot", "req": {
                "coin": coin, "interval": "5m",
                "startTime": int(t.timestamp() * 1000),
                "endTime": int(t2.timestamp() * 1000)}})
            for c in cs:
                out[c["t"]] = float(c["c"])
            t = t2
        return out

    adr_c, ord_c = candles(ADR_COIN), candles(ORD_COIN)
    common = sorted(set(adr_c) & set(ord_c))
    with open(HIST, "w", encoding="utf-8") as f:
        for t in common:
            prem = adr_c[t] / (ord_c[t] * ADR_PER_ORD) - 1
            f.write(json.dumps({"ts": t / 1000, "adr": adr_c[t],
                                "ord": ord_c[t], "prem": round(prem, 6)}) + "\n")
    log(f"백필 완료: {len(common)}개 샘플 ({WINDOW_DAYS}일)")
    rows = load_hist()
    adr, ordi, prem = fetch_premium()
    z, mu, sigma = zscore(rows, prem)
    if z is not None:
        log(f"현재 prem {prem*100:+.2f}% | z = {z:+.2f} | μ {mu*100:.2f}% σ {sigma*100:.2f}%p")


def main():
    args = sys.argv[1:]
    if "--backfill" in args:
        backfill()
        return
    if "--state" in args:
        pos = args[args.index("--state") + 1]
        assert pos in ("flat", "short", "long"), "flat|short|long"
        st = load_state()
        st["pos"] = pos
        save_state(st)
        log(f"포지션 상태 수동 설정: {pos}")
        return
    if "--once" in args:
        run_once()
        return
    log(f"상주 루프 시작 ({INTERVAL_SEC}s 주기)")
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            log(f"[오류] {e}")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
