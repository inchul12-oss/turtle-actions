"""
디바이스-프리 '활성 작업' 1개 단위 — 감시·입력·상태변경을 한 작업이 담당(판호 확정 ②③).
기존 부품 재사용: holding_alerts(판정) · telegram(발송/답장) · state_store(private repo 인계).

한 작업(run_active_job)의 일:
 1) 잠금 획득(활성 작업 1개 제한). 실패 시 아무것도 안 함(동시 발송/쓰기 금지).
 2) 상태 로드: positions(보유)·sent_history(발송이력)·offset(마지막 처리 텔레그램 update_id).
 3) 이벤트 처리(감시 틱 + 텔레그램 체결 입력을 '한 작업 내부'에서):
    - 입력: 이미 처리한 update_id 는 건너뜀 → 파싱 → positions 저장 → 감시 상태(watcher) 갱신 → offset 저장 → "저장 완료" 답장.
    - 틱: 보유 판정 → 이미 보낸 알림(sent) skip, 발송불명(unknown) skip+표시 → 아니면 'sending' 기록 후 발송 → 결과 기록.
      (발송/기록은 그때그때 = 수시 보존. 발송 후 기록 전 중단 시 다음 작업이 'sending'을 불명으로 처리.)
 4) 잠금 해제.

인계: 다음 작업이 같은 저장소를 읽어 보유·offset·발송이력을 이어받는다.
"""
from __future__ import annotations
import os, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import holding_alerts as ha
import telegram_notify as tn


def parse_fill_command(text: str):
    """텔레그램 체결 입력 파싱. 반환 dict 또는 None.
    매수: '매수 SYM shares price N campaign [YYYY-MM-DD] [HH:MM:SS] [tz]'
    매도: '매도 SYM campaign unit price [YYYY-MM-DD]'
    """
    p = text.strip().split()
    if not p:
        return None
    op = p[0]
    try:
        if op in ("매수", "buy", "BUY"):
            sym, shares, price, n, camp = p[1].upper(), p[2], p[3], p[4], p[5]
            d = p[6] if len(p) > 6 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
            t = p[7] if len(p) > 7 else ""
            tz = p[8] if len(p) > 8 else ""
            return {"op": "buy", "symbol": sym, "shares": shares, "price": price,
                    "trade_n": n, "campaign": camp, "date": d, "time": t, "tz": tz}
        if op in ("매도", "sell", "SELL"):
            sym, camp, unit, price = p[1].upper(), p[2], p[3], p[4]
            d = p[5] if len(p) > 5 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
            return {"op": "sell", "symbol": sym, "campaign": camp, "unit": unit, "price": price, "date": d}
    except IndexError:
        return None
    return None


HDR = "symbol,campaign_id,unit_no,fill_date,fill_time,fill_tz,fill_price,shares,trade_n,exit_date,exit_price,note\n"


def apply_fill(pos_text: str, f: dict) -> tuple[str, str]:
    """positions 텍스트에 체결 반영. 반환 (새 텍스트, 확인문구)."""
    import csv, io
    rows = list(csv.DictReader(io.StringIO(pos_text))) if pos_text.strip() else []
    cols = ["symbol", "campaign_id", "unit_no", "fill_date", "fill_time", "fill_tz",
            "fill_price", "shares", "trade_n", "exit_date", "exit_price", "note"]
    if f["op"] == "buy":
        units = [int(float(r.get("unit_no") or 0)) for r in rows if r.get("campaign_id") == f["campaign"]]
        uno = (max(units) + 1) if units else 1
        rows.append({"symbol": f["symbol"], "campaign_id": f["campaign"], "unit_no": uno,
                     "fill_date": f["date"], "fill_time": f["time"], "fill_tz": f["tz"],
                     "fill_price": f["price"], "shares": f["shares"], "trade_n": f["trade_n"],
                     "exit_date": "", "exit_price": "", "note": "telegram 입력"})
        note = f"매수 {f['symbol']} {f['campaign']} U{uno} {f['shares']}주 @{f['price']} N={f['trade_n']}"
    else:  # sell
        note = f"매도 {f['symbol']} {f['campaign']} U{f['unit']} @{f['price']}"
        hit = False
        for r in rows:
            if r.get("campaign_id") == f["campaign"] and str(int(float(r.get("unit_no") or 0))) == str(f["unit"]):
                r["exit_date"] = f["date"]; r["exit_price"] = f["price"]; hit = True
        if not hit:
            return pos_text, f"[무시] 해당 유닛 없음: {note}"
    out = io.StringIO(); w = csv.DictWriter(out, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})
    return out.getvalue(), note


def run_active_job(store, run_id: str, feed, telegram, today_et: str,
                   exit_levels: dict = None, test: bool = True) -> dict:
    exit_levels = exit_levels or {}
    summ = {"run_id": run_id, "skipped": None, "sent": [], "dup_skip": [], "ambiguous": [],
            "inputs": [], "final_offset": None, "lock": False}
    if not store.acquire_lock(run_id):
        summ["skipped"] = "다른 활성 작업 있음(잠금 실패)"
        return summ
    summ["lock"] = True
    try:
        def build():
            fills = ha.load_fills(store.pos)
            camps = {s: ha.build_campaign(s, fl, exit_levels.get(s), today_et) for s, fl in fills.items()}
            return camps, ha.HoldingWatcher(camps, today_et)
        camps, watcher = build()
        offset = store.get_offset()
        for ev in feed:
            kind = ev[0]
            if kind == "input":
                _, uid, text = ev
                if uid < offset:
                    continue                     # 이미 처리한 입력(반복 안 함)
                f = parse_fill_command(text)
                if f:
                    newtext, note = apply_fill(store.load_positions_text(), f)
                    store.save_positions_text(newtext)      # 수시 보존
                    camps, watcher = build()                 # 감시 상태 갱신
                    telegram.reply(f"저장 완료 · {note}")     # 완료 답장(대기 아님, 즉시 반영)
                    summ["inputs"].append({"uid": uid, "note": note})
                offset = uid + 1
                store.set_offset(offset)                     # 처리 위치 수시 보존
            elif kind == "tick":
                _, sym, px, dt = ev
                for al in watcher.observe(sym, float(px), dt):
                    if al.key in store.sent_keys():
                        summ["dup_skip"].append(al.key); continue      # 이미 발송(인계 중복 방지)
                    if al.key in store.unknown_keys():
                        summ["ambiguous"].append(al.key); continue     # 발송 불명 → 자동 재발송 안 함
                    camp = camps[sym]
                    store.record(sym, today_et, "sending", al.key)     # 발송 직전 기록
                    status = telegram.send(tn.build_holding_message(al, camp.n, dt, test=test))
                    store.record(sym, today_et, status, al.key)        # 발송 결과 기록(수시)
                    if status == "sent":
                        summ["sent"].append(al.key)
        summ["final_offset"] = offset
    finally:
        store.release_lock(run_id)
    return summ
