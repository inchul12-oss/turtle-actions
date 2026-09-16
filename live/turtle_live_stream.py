"""
터틀 진입 후보 실시간 표시 — 기존 도구 연결. 야후 스트리밍(yahoo_stream_test_v4 의 31연결 수신부 재사용) + turtle_levels 가 만든 기준가표(levels.csv) 를 붙여,
'야후 수신 가격 ≥ 진입 기준가' 종목을 표시한다. 자동 주문 없음, 신규 진입 후보 표시까지.

판정 규칙(판호 지시):
- 상태 '정상'으로 계산된 진입 기준가에 대해서만 판정. '확인 필요'·기준가 없음은 제외(집계만).
- **오늘(미국 동부) 체결 시각 메시지만** 판정에 사용 → 옛 스냅샷을 현재 돌파 신호로 쓰지 않는다.
- 같은 종목 같은 날 진입 신호는 **최초 1회만 이벤트 기록**. 이후 가격 갱신은 이벤트로 내지 않는다.
- 그 종목을 오늘 처음 수신했을 때 이미 기준가 이상이면 first_obs_already_over=True → '첫 관측부터 기준가 이상'(실제 돌파 순간을 포착했다고 표시하지 않음). 관측 중 기준가를 넘어서면 False.
출력(최소): 터미널에 이벤트 한 줄씩 + out_yahoo/entry_events_<UTC>.csv (종목/기준가/수신가/시세시각/데이터상태/first_obs/conn).
요약: 기준가 계산 가능(정상) / 확인 필요 / 시세 미수신 수.

실행: python3 turtle_live_stream.py --levels out/levels_full.csv --symbols-file nasdaq_common.txt --per-conn 100 --minutes 30
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import threading
import time
from collections import Counter
from datetime import datetime, timezone, timedelta, date

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out_yahoo")

# 미국 증시 휴장일(2026, NYSE/Nasdaq). 기대 최종 완결 거래일 계산용(주말+휴장 제외).
KNOWN_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def expected_last_completed_trading_day(today: date) -> date:
    """오늘(미국 ET) 기준 '마지막으로 완결됐어야 할 거래일' = 오늘 직전 거래일(주말·휴장 제외).
    당일 봉은 장중 미완성이라 기준가에 안 쓰므로, 최신 기준가표의 as_of 는 이 날짜여야 한다."""
    d = today - timedelta(days=1)
    while d.weekday() >= 5 or d.isoformat() in KNOWN_HOLIDAYS:
        d -= timedelta(days=1)
    return d


def load_levels(path: str) -> dict:
    """levels.csv → {sym: {entry, status, as_of, exit_level}}. status=='정상' 이고 entry 유효한 것만 진입 판정 대상.
    exit_level(L20−틱)은 보유관리 L20 청산 판정에 재사용."""
    lv = {}
    def _f(x):
        try:
            return float(x) if x not in (None, "", "nan") else float("nan")
        except ValueError:
            return float("nan")
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            lv[r["symbol"]] = {"entry": _f(r.get("entry_level")), "status": r.get("status", ""),
                               "as_of": r.get("as_of", ""), "exit_level": _f(r.get("exit_level"))}
    return lv


def basis_date(levels: dict) -> str:
    """기준가 계산 기준일(정상 종목의 as_of 최빈값). 표시용."""
    from collections import Counter
    c = Counter(v.get("as_of") for v in levels.values() if v["status"] == "정상" and v.get("as_of"))
    return c.most_common(1)[0][0] if c else "?"


def et_date(ts_ms: int) -> str:
    return (datetime.fromtimestamp(ts_ms / 1000, timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")


def load_security_class(path: str) -> dict:
    """security_class.csv → {sym: klass}. klass ∈ {non_common, unverified}.
    목록에 없는 심볼은 '보통주(잠정)'로 본다. '가격 계산 가능'과 별개의 '보통주 확인' 축."""
    sc = {}
    if not path or not os.path.exists(path):
        return sc
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            s = (r.get("symbol") or "").strip()
            k = (r.get("klass") or "").strip()
            if s and k:
                sc[s] = k
    return sc


def run(symbols, levels, per_conn, minutes, gap=1.0, join_timeout=5.0, sec_class=None, notify=True, positions_path=None, test=False):
    import yfinance as yf
    sec_class = sec_class or {}
    os.makedirs(OUT, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ev_path = os.path.join(OUT, f"entry_events_{stamp}.csv")
    raw_path = os.path.join(OUT, f"stream_live_{stamp}.jsonl")
    sum_path = os.path.join(OUT, f"live_summary_{stamp}.json")
    today_et = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")

    tradable_all = {s for s, v in levels.items() if v["status"] == "정상" and v["entry"] == v["entry"]}
    need_check = {s for s, v in levels.items() if not (v["status"] == "정상" and v["entry"] == v["entry"])}
    # 기준가 유효기간 검사: as_of 가 기대 최종 완결 거래일과 정확히 '일치'하는 기준가만 판정에 사용.
    # 오래된 날짜뿐 아니라 미래 날짜도 정상으로 허용하지 않는다 → '기준가 갱신 필요'로 표시.
    exp = expected_last_completed_trading_day(date.fromisoformat(today_et)).isoformat()
    expected_lc = date.fromisoformat(exp)
    stale_old = {s for s in tradable_all if (levels[s].get("as_of") or "") < exp}
    stale_future = {s for s in tradable_all if (levels[s].get("as_of") or "") > exp}
    stale = stale_old | stale_future
    tradable = tradable_all - stale
    basis = basis_date(levels)   # 기준가 계산 기준일(표시·알림용) — 조기 계산

    # 종목 종류(보통주 확인) 축 — '가격 계산 가능'과 별도 상태.
    # common_ok=True 는 '개별 확인된 보통주'(klass=common)뿐.
    # 대상 외(non_common) / 정체 미확인(unverified) / 아직 미조회(분류표에 없음) 는 전부 common_ok=False.
    # 미조회 종목은 삭제하지 않고 '종류 미확인'으로 유지(집계·표시만).
    confirmed_common = {s for s, k in sec_class.items() if k == "common"}
    noncommon = {s for s, k in sec_class.items() if k == "non_common"}
    unverified = {s for s, k in sec_class.items() if k == "unverified"}
    tradable_noncommon = tradable & noncommon                                   # 가격O, 대상 외
    tradable_unverified = tradable & unverified                                  # 가격O, 조회했으나 미확보
    tradable_confirmed = tradable & confirmed_common                            # 가격O, 보통주 확인
    tradable_unqueried = tradable - noncommon - unverified - confirmed_common    # 가격O, 미조회 = 종류 미확인
    common_ok = tradable_confirmed                                              # 확정 후보 풀 = 가격 계산 가능 AND 보통주 확인

    # ---- 보유관리(통합지시 3): 실제 체결(positions.csv 또는 Secret) 기반 add/stop/L20 알림 ----
    import holding_alerts as haal
    fills_by_sym = haal.load_fills(positions_path)          # 로컬 CSV 또는 POSITIONS_CSV_B64(Secret)
    hold_campaigns = {}; hold_flags = {}
    for hsym, fl in fills_by_sym.items():
        exlv = levels.get(hsym, {}).get("exit_level")
        exlv = exlv if (exlv is not None and exlv == exlv) else None      # NaN→None
        camp = haal.build_campaign(hsym, fl, exlv, today_et)
        hold_campaigns[hsym] = camp
        if not camp.ok:
            hold_flags[hsym] = camp.reason
    held_syms = {s for s, c in hold_campaigns.items() if c.ok}
    hold_watcher = haal.HoldingWatcher(hold_campaigns, today_et) if hold_campaigns else None
    for s in held_syms:                                     # 보유 종목은 후보 풀과 무관하게 수신 대상에 포함
        if s not in symbols:
            symbols.append(s)

    groups = [symbols[i:i + per_conn] for i in range(0, len(symbols), per_conn)]
    counts = Counter(); today_counts = Counter(); first_today_price = {}; lat = {}
    events = []; hold_events = []; signaled = set(); lock = threading.Lock()
    import telegram_notify as tn
    if not notify:
        notifier = None
    elif test:      # 시험 모드: 보유 메시지 [테스트] 접두 + 별도 이력, 실 진입알림 발송 안 함
        notifier = tn.Notifier(OUT, today_et, log_name="telegram_log_holdtest.jsonl", hist_name="sent_history_holdtest.csv")
    else:
        notifier = tn.Notifier(OUT, today_et)
    raw = open(raw_path, "w")
    evf = open(ev_path, "w", newline="")
    evw = csv.DictWriter(evf, fieldnames=["recv_utc", "symbol", "entry_level", "recv_price", "msg_time_utc", "msg_time_et", "data_status", "universe_class", "first_obs_already_over", "conn"])
    evw.writeheader()

    def make_handler(conn_no):
        def handler(msg):
            try:
                if not isinstance(msg, dict):
                    msg = dict(msg)
                sym = msg.get("id") or msg.get("symbol")
                now_dt = datetime.now(timezone.utc); now = now_dt.isoformat()
                t = msg.get("time"); is_today = False; msg_et = None; l = None
                try:
                    tms = int(str(t)); msg_et = et_date(tms); is_today = msg_et == today_et
                    l = (now_dt - datetime.fromtimestamp(tms / 1000, timezone.utc)).total_seconds()
                except Exception:
                    pass
                px = msg.get("price")
                with lock:
                    raw.write(json.dumps({"recv_utc": now, "conn": conn_no, "id": sym, "price": px, "time": t}) + "\n")
                    counts[sym] += 1
                    if not is_today:
                        return                       # 옛 스냅샷은 판정에서 제외
                    today_counts[sym] += 1
                    if l is not None:
                        lat.setdefault(sym, []).append(l)
                    # 보유관리 알림(후보 풀과 독립). observe 가 체결시각 이후 틱만 판정.
                    if hold_watcher and px is not None and sym in held_syms:
                        try:
                            _ot = datetime.fromtimestamp(int(str(t)) / 1000, timezone.utc)   # 시세(관측) 시각
                            for al in hold_watcher.observe(sym, float(px), _ot):
                                camp = hold_campaigns[sym]
                                stg = (notifier.enqueue(sym, tn.build_holding_message(al, camp.n, _ot, test=test), dedup_key=al.key)
                                       if (notifier and notifier.enabled) else "off")
                                hold_events.append({"recv_utc": now, "symbol": sym, "kind": al.kind, "text_kind": al.text_kind,
                                                    "line": al.line, "recv_price": float(px), "units": "/".join(al.units),
                                                    "already": al.already, "telegram": stg, "conn": conn_no})
                                print(f"  ★ 보유관리 {sym} {al.text_kind}  관측 {px} 기준 {al.line}  [{'첫관측부터' if al.already else '관측중'}]  {now[11:19]}", flush=True)
                        except Exception:
                            pass
                    lv = levels.get(sym)
                    if px is None or not lv or lv["status"] != "정상" or lv["entry"] != lv["entry"]:
                        return
                    if sym in stale:                 # 기준가 갱신 필요(오래된 기준가) → 판정 제외
                        return
                    if sym not in confirmed_common:  # 개별 확인된 보통주만 확정 후보 (대상 외·미확인·미조회 제외)
                        return
                    px = float(px)
                    if sym not in first_today_price:
                        first_today_price[sym] = px   # 오늘 첫 수신가
                    if px >= lv["entry"] and sym not in signaled:
                        signaled.add(sym)
                        already = first_today_price[sym] >= lv["entry"]
                        _mu = datetime.fromtimestamp(int(str(t)) / 1000, timezone.utc)  # 시세(체결) 시각
                        ev = {"recv_utc": now, "symbol": sym, "entry_level": lv["entry"], "recv_price": px,
                              "msg_time_utc": _mu.strftime("%Y-%m-%d %H:%M:%S"),
                              "msg_time_et": (_mu - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
                              "data_status": "정상", "universe_class": "보통주", "first_obs_already_over": already, "conn": conn_no}
                        events.append(ev); evw.writerow(ev); evf.flush()
                        tag = "첫 관측부터 기준가 이상" if already else "관측 중 기준가 도달"
                        print(f"  ▶ 진입후보 {sym}  수신 {px} ≥ 기준 {lv['entry']}  [{tag}]  {now[11:19]}", flush=True)
                        if notifier and notifier.enabled and not test:   # 시험모드에선 실 진입알림 발송 안 함(보유 연결시험 격리)
                            st = notifier.enqueue(sym, tn.build_message(sym, lv["entry"], px, basis, _mu, already))
                            ev["telegram"] = st
            except Exception as e:
                with lock:
                    raw.write(json.dumps({"recv_utc": datetime.now(timezone.utc).isoformat(), "handler_error": f"{type(e).__name__}: {e}"}) + "\n")
        return handler

    def listen_wrap(c, i):
        c["rec"]["listen_started_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            c["ws"].listen(make_handler(i))
        except Exception as e:
            c["rec"]["listen_error"] = f"{type(e).__name__}: {e}"
        finally:
            c["rec"]["listen_ended_utc"] = datetime.now(timezone.utc).isoformat()

    t0 = time.time(); conns = []
    for i, g in enumerate(groups, 1):
        rec = {"conn": i, "requested": len(g), "subscribe_error": None, "listen_error": None}
        c = {"rec": rec, "ws": None, "group": g, "thread": None}
        try:
            ws = yf.WebSocket(verbose=False); c["ws"] = ws
            ws.subscribe(g)
            th = threading.Thread(target=listen_wrap, args=(c, i), daemon=True); th.start(); c["thread"] = th
        except Exception as e:
            rec["subscribe_error"] = f"{type(e).__name__}: {e}"
        conns.append(c)
        if i < len(groups):
            time.sleep(gap)
    obs_start = time.time()
    basis = basis_date(levels)
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_et = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%H:%M ET")
    fresh = basis == exp
    fresh_note = "최신(기대일과 일치)" if fresh else f"불일치 → 갱신 필요"
    print(f"[기준가 계산 기준일] {basis} (마지막 완결 일봉) · 기대 최종 완결 거래일 {exp} → {fresh_note}", flush=True)
    if stale:
        print(f"[기준가 갱신 필요] 판정 제외 {len(stale)}종목 (오래됨 {len(stale_old)} / 미래 {len(stale_future)}) 예: {', '.join(sorted(stale)[:8])}", flush=True)
    print(f"[감시 시작] {now_local} 로컬 / {now_et} · {len(conns)}개 연결 / {minutes}분", flush=True)
    print(f"  · 가격 계산 가능(정상·기대일 일치): {len(tradable)}  / 확인 필요: {len(need_check)}  / 기준가 갱신 필요: {len(stale)}", flush=True)
    print(f"  · 보통주 확인: {len(common_ok)}  / 대상 외(비보통주): {len(tradable_noncommon)}  / 정체 미확인: {len(tradable_unverified)}  / 미조회(종류 미확인): {len(tradable_unqueried)}", flush=True)
    print(f"  · 확정 후보 대상 = 가격 계산 가능 AND 보통주 확인 = {len(common_ok)}종목 (대상 외·미확인·미조회는 후보 제외, 목록은 유지)", flush=True)
    end = obs_start + minutes * 60
    while time.time() < end:
        time.sleep(10)
        with lock:
            alive = sum(1 for c in conns if c["thread"] and c["thread"].is_alive())
            rcv_today = len(today_counts & Counter(dict.fromkeys(tradable, 1)))  # 정상 종목 중 오늘 수신 수
            print(f"  {int(time.time()-obs_start):4d}s  연결 {alive}/{len(conns)}  정상종목 오늘수신 {sum(1 for s in tradable if today_counts.get(s,0)>0)}/{len(tradable)}  진입후보 {len(events)}", flush=True)
    for c in conns:
        try:
            if c["ws"]:
                c["ws"].close()
        except Exception:
            pass
    not_joined = []
    for c in conns:
        if c["thread"]:
            c["thread"].join(timeout=join_timeout)
            if c["thread"].is_alive():
                not_joined.append(c["rec"]["conn"])
    raw.close(); evf.close()
    tel = None
    if notifier:
        tel_stats, tel_alive = notifier.stop()
        tel = {"enabled": notifier.enabled, "stats": tel_stats, "worker_not_joined": tel_alive,
               "history": notifier.hist_path if notifier.enabled else None}

    tradable_recv_today = sum(1 for s in tradable if today_counts.get(s, 0) > 0)
    summary = {
        "started_utc": datetime.fromtimestamp(t0, timezone.utc).isoformat(), "today_et": today_et,
        "basis_date": basis_date(levels),
        "expected_last_completed": exp,
        "basis_current": basis == exp,
        "stale_excluded": len(stale), "stale_excluded_old": len(stale_old), "stale_excluded_future": len(stale_future),
        "stale_symbols_sample": sorted(stale)[:20],
        "minutes": minutes, "requested_symbols": len(symbols), "connections": len(groups), "per_connection": per_conn,
        "levels_total": len(levels), "tradable_normal_before_stale": len(tradable_all),
        "tradable_normal": len(tradable), "need_check": len(need_check),
        "price_ok": len(tradable), "common_confirmed": len(common_ok),
        "security_non_common": len(tradable_noncommon), "security_unverified": len(tradable_unverified),
        "security_unqueried": len(tradable_unqueried),
        "candidate_pool": len(common_ok),
        "tradable_received_today": tradable_recv_today, "tradable_no_message_today": len(tradable) - tradable_recv_today,
        "entry_candidates": len(events),
        "entry_first_obs_already_over": sum(1 for e in events if e["first_obs_already_over"]),
        "entry_crossed_during_watch": sum(1 for e in events if not e["first_obs_already_over"]),
        "threads_not_joined": not_joined, "messages_total": sum(counts.values()),
        "holding_watched": sorted(held_syms), "holding_alerts": len(hold_events),
        "holding_alerts_detail": hold_events,
        "holding_needs_input": hold_flags,   # N 입력 필요 / 체결시각 입력 필요 / campaign_id 누락 등
        "holding_needs_time_runtime": (hold_watcher.needs_time_flagged if hold_watcher else {}),
        "telegram": tel,
        "files": {"events": ev_path, "raw": raw_path},
        "note": "자동 주문 없음. 오늘 체결 메시지만 판정. 옛 스냅샷 제외. 신호는 종목·날짜당 최초 1회. 야후 실시간=미국 거래량 일부 → 첫 틱 보장 아님. 미수신 종목은 이번 세션에 도착 안 한 것일 뿐 향후 수신을 보장하지 않음",
    }
    json.dump(summary, open(sum_path, "w"), indent=1, ensure_ascii=False)
    print("\n=== 요약 ===")
    print(f"기준가 계산 기준일: {basis}  |  기대 최종 완결 거래일: {exp}  |  {'일치(최신)' if fresh else '불일치(갱신 필요)'}")
    print(f"[가격] 계산 가능(정상·기대일 일치): {len(tradable)}  |  기준가 갱신 필요: {len(stale)} (오래됨 {len(stale_old)}/미래 {len(stale_future)})  |  확인 필요: {len(need_check)}")
    print(f"[종류] 보통주 확인: {len(common_ok)}  |  대상 외: {len(tradable_noncommon)}  |  정체 미확인: {len(tradable_unverified)}  |  미조회(종류 미확인): {len(tradable_unqueried)}   ← '가격 계산 가능'과 별도 축")
    print(f"확정 후보 대상(가격가능 AND 보통주확인): {len(common_ok)}  |  그중 오늘 시세 수신: {tradable_recv_today}  미수신: {len(tradable)-tradable_recv_today} (이번 세션 미도착, 향후 수신 보장 아님)")
    print(f"진입 후보(오늘, 최초 1회, 보통주만): {len(events)}  (첫 관측부터 기준가 이상 {summary['entry_first_obs_already_over']} / 관측 중 도달 {summary['entry_crossed_during_watch']})")
    print(f"[보유관리] 감시 {len(held_syms)}종목 · 알림 {len(hold_events)}건"
          + (f" · 입력 필요 {len(hold_flags)}종목({', '.join(f'{k}:{v}' for k,v in list(hold_flags.items())[:4])})" if hold_flags else "")
          + (f" · 체결시각 필요 {len(hold_watcher.needs_time_flagged)}" if (hold_watcher and hold_watcher.needs_time_flagged) else ""))
    print(f"이벤트 CSV: {ev_path}")
    if tel is not None:
        if tel["enabled"]:
            s = tel["stats"]
            print(f"[텔레그램] 발송 {s.get('sent',0)} / 실패 {s.get('failed',0)} / 불명(중복가능) {s.get('unknown',0)} / 중복skip {s.get('dup_skip',0)}"
                  + ("  ※워커 미정리" if tel["worker_not_joined"] else ""), flush=True)
        else:
            print("[텔레그램] 미설정(토큰/chat_id 없음) → 발송 비활성, 감시·CSV는 정상", flush=True)
    if not_joined:
        print("정리 안 된 연결:", not_joined)
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default=os.path.join(HERE, "out", "levels_full.csv"), help="turtle_levels 가 만든 기준가표 CSV")
    ap.add_argument("--symbols-file", default=os.path.join(HERE, "nasdaq_common.txt"))
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--per-conn", type=int, default=100)
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--gap", type=float, default=1.0)
    ap.add_argument("--security-class", default=os.path.join(HERE, "security_class.csv"), help="종목 종류 분류표(보통주 확인/대상 외/미확인)")
    ap.add_argument("--positions", default=os.path.join(HERE, "positions.csv"), help="실제 체결 CSV(보유관리 알림용). 없거나 POSITIONS_CSV_B64(Secret) 있으면 그쪽 사용")
    ap.add_argument("--no-telegram", action="store_true", help="텔레그램 발송 끄기(감시만)")
    ap.add_argument("--test", action="store_true", help="시험모드: 보유 알림 [테스트] 접두+별도 이력, 실 진입알림 발송 안 함")
    a = ap.parse_args()
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",")]
    else:
        syms = [l.strip().upper() for l in open(a.symbols_file) if l.strip() and not l.startswith("#")]
    if not os.path.exists(a.levels):
        raise SystemExit(f"기준가표 없음: {a.levels} — 먼저 yahoo_daily.py → turtle_levels.py 로 생성")
    run(syms, load_levels(a.levels), a.per_conn, a.minutes, gap=a.gap, sec_class=load_security_class(a.security_class), notify=not a.no_telegram, positions_path=a.positions, test=a.test)
