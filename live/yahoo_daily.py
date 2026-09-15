"""
야후(yfinance) 일봉 확보 — 인철 맥에서 실행. 목록 전체의 '계산용 일봉'을 기존 형식으로 저장 → turtle_levels.py 가 그대로 읽어 H55·N·기준가 계산.
- 저장 형식: data/prices_yahoo/<SYM>.raw.csv  ("datetime;open;high;low;close;volume", 최신순) + <SYM>.meta.json  (기존 prices_td 와 동일 형식 → 도구 재사용)
- **미조정 가격**(auto_adjust=False): 야후 실시간 스트리밍 가격과 같은 스케일로 비교하기 위함. 분할 반영 여부는 turtle_levels 의 check_symbol(PRICE_JUMP 등)이 판정 → 최근 분할 종목은 '확인 필요'로 남는다.
- **당일 미완성 봉 제외**: 오늘(미국 동부) 날짜 행은 저장하지 않는다(다음 거래일 기준가 계산에 장중 봉을 섞지 않음).
- 요청 제한(429/rate) 시 대기·재시도. 최종 실패 종목은 out_yahoo/daily_failures.csv 에 사유와 함께 기록.
실행: python3 yahoo_daily.py --symbols-file nasdaq_common.txt         # 전체
      python3 yahoo_daily.py --symbols AAPL,KLAC,PFG                  # 일부
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
OUTDIR = os.path.join(ROOT, "data", "prices_yahoo")
FAILDIR = os.path.join(HERE, "out_yahoo")


def today_et_str() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")


def df_to_rows(df):
    """df(yfinance history, 시간오름차순) → [(date,o,h,l,c,v)] 최신순, 당일·미래·NaN 제외."""
    today = today_et_str()
    rows = []
    for ts, r in df.iterrows():
        d = ts.strftime("%Y-%m-%d")
        if d >= today:                      # 오늘·미래(장중 미완성) 봉 제외
            continue
        o, h, l, c, v = r["Open"], r["High"], r["Low"], r["Close"], r["Volume"]
        if any(x != x for x in (o, h, l, c)):   # NaN 행 건너뜀
            continue
        rows.append((d, o, h, l, c, int(v) if v == v else 0))
    rows.sort(key=lambda x: x[0], reverse=True)     # 최신순
    return rows


def read_existing(sym: str):
    """기존 raw.csv → ([(date,o,h,l,c,v)] 최신순, 최신날짜) 또는 ([], None). 재사용용."""
    p = os.path.join(OUTDIR, f"{sym}.raw.csv")
    if not os.path.exists(p):
        return [], None
    rows = []
    with open(p) as f:
        next(f, None)  # 헤더
        for line in f:
            parts = line.strip().split(";")
            if len(parts) != 6:
                continue
            d, o, h, l, c, v = parts
            try:
                rows.append((d, float(o), float(h), float(l), float(c), int(float(v))))
            except ValueError:
                continue
    rows.sort(key=lambda x: x[0], reverse=True)
    return rows, (rows[0][0] if rows else None)


def write_rows(sym: str, rows, source: str) -> dict:
    """rows(최신순) → raw.csv + meta 저장. rows 비면 예외."""
    if not rows:
        raise ValueError("no_completed_bars(당일 제외 후 0행)")
    today = today_et_str()
    p = os.path.join(OUTDIR, f"{sym}.raw.csv")
    with open(p, "w") as f:
        f.write("datetime;open;high;low;close;volume\n")
        for d, o, h, l, c, v in rows:
            f.write(f"{d};{o};{h};{l};{c};{v}\n")
    meta = {"symbol": sym, "source": source,
            "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "rows": len(rows), "first_bar": rows[-1][0], "last_bar": rows[0][0],
            "adjusted": "no (raw/unadjusted; 분할은 check_symbol 이 판정)", "excluded_today": today}
    json.dump(meta, open(os.path.join(OUTDIR, f"{sym}.meta.json"), "w"), indent=1, ensure_ascii=False)
    return meta


def save_series(sym: str, df, period: str) -> dict:
    """df(전체) → raw.csv 전체 재작성(전체 모드)."""
    return write_rows(sym, df_to_rows(df),
                      f"yfinance history(period={period}, auto_adjust=False, interval=1d)")


def merge_update(sym: str, df) -> dict:
    """증분: 기존 raw 를 재사용하고 df(최근 창)에서 기존 최신일보다 뒤인 완결 봉만 이어붙인다.
    기존 봉은 건드리지 않음(재다운로드 최소화). 기존 파일이 없거나 부실하면 전체로 저장."""
    existing, last = read_existing(sym)
    recent = df_to_rows(df)
    if not existing or len(existing) < 55:      # 기존이 없거나 55봉 미만 → 최근 창 전체 저장(전체 재확보는 상위에서)
        return write_rows(sym, recent, "yfinance history(update; 기존 부족 → 최근 창 저장)"), 0
    new = [r for r in recent if r[0] > last]     # 기존 최신일보다 뒤인 완결 봉만
    merged = existing + new
    merged.sort(key=lambda x: x[0], reverse=True)
    meta = write_rows(sym, merged, f"yfinance history(update; 기존 {len(existing)}봉 재사용 + {len(new)}봉 추가)")
    return meta, len(new)


def fetch_one(yf, sym: str, period: str, max_retry: int = 3):
    last_err = None
    for attempt in range(1, max_retry + 1):
        try:
            df = yf.Ticker(sym).history(period=period, interval="1d", auto_adjust=False, actions=False)
            if df is None or len(df) == 0:
                return None, "no_data"
            return df, None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            msg = str(e).lower()
            if "429" in msg or "rate" in msg or "too many" in msg:
                wait = 10 * attempt
                print(f"    [{sym}] rate limit → {wait}s 대기 후 재시도({attempt}/{max_retry})", flush=True)
                time.sleep(wait)
            else:
                time.sleep(1.5 * attempt)
    return None, last_err


def run(symbols: list, period: str, pause: float):
    import yfinance as yf
    os.makedirs(OUTDIR, exist_ok=True); os.makedirs(FAILDIR, exist_ok=True)
    ok, fail = 0, []
    t0 = time.time()
    for i, sym in enumerate(symbols, 1):
        df, err = fetch_one(yf, sym, period)
        if err:
            fail.append({"symbol": sym, "error": err})
        else:
            try:
                m = save_series(sym, df, period)
                ok += 1
            except Exception as e:
                fail.append({"symbol": sym, "error": f"save: {type(e).__name__}: {e}"})
        if i % 50 == 0 or i == len(symbols):
            el = int(time.time() - t0)
            print(f"  {i}/{len(symbols)}  저장 {ok}  실패 {len(fail)}  {el}s", flush=True)
        time.sleep(pause)
    # 실패 기록
    import csv
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fp = os.path.join(FAILDIR, f"daily_failures_{stamp}.csv")
    with open(fp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "error"]); w.writeheader(); w.writerows(fail)
    print(f"\n일봉 저장 완료: 성공 {ok} / 실패 {len(fail)} / 요청 {len(symbols)}")
    print(f"저장 위치: {OUTDIR}")
    print(f"실패 목록: {fp}")
    if fail:
        print("실패 예시:", ", ".join(f"{d['symbol']}({d['error'][:30]})" for d in fail[:8]))
    print("\n다음: python3 turtle_levels.py --symbols-file <목록> --data-dir ../data/prices_yahoo --out levels_full  → 전체 기준가표")


def _days_between(d1: str, d2: str) -> int:
    from datetime import date as _date
    return abs((_date.fromisoformat(d2) - _date.fromisoformat(d1)).days)


def run_update(symbols: list, recent_period: str, full_period: str, pause: float):
    """증분 갱신: 기존 raw 가 충분하면 최근 창만 받아 누락 완결 봉만 이어붙인다(재다운로드 최소화).
    기존이 없거나 부실하거나 너무 오래됐으면 전체(full_period) 확보로 대체."""
    import yfinance as yf
    os.makedirs(OUTDIR, exist_ok=True); os.makedirs(FAILDIR, exist_ok=True)
    ok = filled = already = full_done = 0
    fail = []
    today = today_et_str()
    t0 = time.time()
    for i, sym in enumerate(symbols, 1):
        existing, last = read_existing(sym)
        use_full = (not existing) or len(existing) < 55 or (last and _days_between(last, today) > 40)
        if not use_full:                                   # 증분: 최근 창만
            df, err = fetch_one(yf, sym, recent_period)
            if err:
                fail.append({"symbol": sym, "error": f"update: {err}"})
            else:
                try:
                    _m, n = merge_update(sym, df); ok += 1
                    filled += 1 if n > 0 else 0
                    already += 1 if n == 0 else 0
                except Exception as e:
                    fail.append({"symbol": sym, "error": f"merge: {type(e).__name__}: {e}"})
        else:                                              # 전체 확보
            df, err = fetch_one(yf, sym, full_period)
            if err:
                fail.append({"symbol": sym, "error": f"full: {err}"})
            else:
                try:
                    save_series(sym, df, full_period); ok += 1; full_done += 1
                except Exception as e:
                    fail.append({"symbol": sym, "error": f"save: {type(e).__name__}: {e}"})
        if i % 50 == 0 or i == len(symbols):
            print(f"  {i}/{len(symbols)}  갱신 {ok} (새봉추가 {filled}/이미최신 {already}/전체확보 {full_done})  실패 {len(fail)}  {int(time.time()-t0)}s", flush=True)
        time.sleep(pause)
    import csv
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fp = os.path.join(FAILDIR, f"daily_failures_{stamp}.csv")
    with open(fp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "error"]); w.writeheader(); w.writerows(fail)
    print(f"\n일봉 증분 갱신 완료: 성공 {ok} (새 완결봉 추가 {filled} / 이미 최신 {already} / 전체 재확보 {full_done}) / 실패 {len(fail)} / 요청 {len(symbols)}")
    print(f"저장 위치: {OUTDIR}")
    print(f"실패 목록: {fp}")
    if fail:
        print("실패 예시:", ", ".join(f"{d['symbol']}({d['error'][:30]})" for d in fail[:8]))
    print("\n다음: python3 turtle_levels.py --symbols-file <목록> --data-dir ../data/prices_yahoo --out levels_full  → 전체 기준가표")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None, help="쉼표 구분")
    ap.add_argument("--symbols-file", default=os.path.join(HERE, "nasdaq_common.txt"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--period", default="500d", help="확보 기간(캘린더 500일≈1년4개월; H55·N·1년 점검에 충분)")
    ap.add_argument("--update", action="store_true", help="증분 갱신(기존 재사용, 누락 완결봉만 추가). 매일 갱신용")
    ap.add_argument("--recent-period", default="1mo", help="증분 모드에서 받을 최근 창(기존 최신일 이후만 병합)")
    ap.add_argument("--pause", type=float, default=0.25, help="종목 간 간격(초). rate limit 나면 늘릴 것")
    a = ap.parse_args()
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",")]
    else:
        syms = [l.strip().upper() for l in open(a.symbols_file) if l.strip() and not l.startswith("#")]
    if a.limit:
        syms = syms[:a.limit]
    if a.update:
        run_update(syms, a.recent_period, a.period, a.pause)
    else:
        run(syms, a.period, a.pause)
