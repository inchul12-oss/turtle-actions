"""
터틀 System 2 — 실사용 최소 도구: 종목별 진입·추가매수·손절·청산 기준가 표 생성.

    python live/turtle_levels.py                      # data/prices_td/*.raw.csv (확보한 일봉) + live/positions.csv → live/out/levels.md / .csv
    python live/turtle_levels.py --symbols PFG,KLAC   # 일부만

규칙(원문으로 확인된 것, 변경 없음): 진입 = 직전 55일 고가(당일 제외) + 1틱 장중 돌파 / 청산 = 직전 20일 저가 − 1틱 /
손절 = 체결가 − 2N, 유닛 추가마다 기존 유닛 손절 +½N / 추가매수 = 마지막 체결가 + ½N, 종목 4유닛 /
유닛 수량 = floor(A × 1% / N) (A = 명목계좌, 입력 없으면 수량은 계산하지 않고 기준가만 표시 — 계좌 정보 없다고 신호를 숨기지 않는다).

원형 미확정(판호 미승인) 가정 — 이 도구가 쓰는 방식:
  [D-1/D-20] 손절·추가 간격에 쓰는 N = positions.csv 의 trade_n(캠페인 N). 비어 있으면 첫 유닛 체결일 전일까지의 N 을 채우고 'assumed' 표시.
  실제 보유·체결은 인철이 live/positions.csv 에 직접 입력한 것만 쓴다. 모의계좌 체결은 넣지 않는다.

데이터 상태: step2.check_connection 의 플래그로 '정상' / '확인 필요' 를 나눈다. 확인 필요 행은 기준가를 표시하되 정상 신호로 취급하지 않는다.
틱: TurtleConfig.tick_size(0.01). $1 미만 가격(호가 0.0001)·분할 조정가는 '확인 필요' 또는 '격자 주의' 로 표시만 한다(임의 반올림 없음).
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
from turtle.config import TurtleConfig                                   # noqa: E402
from turtle.indicators import n_value                                     # noqa: E402
from step2.td_source import get_series, load_raw                          # noqa: E402
from step2.check_connection import check_symbol, consensus_calendar, ASSUMED_US_HOLIDAYS   # noqa: E402

CFG = TurtleConfig()
TICK = CFG.tick_size
POSITIONS_CSV = os.path.join(HERE, "positions.csv")
ACCOUNT_JSON = os.path.join(HERE, "account.json")
OUT = os.path.join(HERE, "out")

# TradingView 심볼 메타(session_holidays, 2026-09-13 실측)에서 확인된 이후 휴장일 — 기대 최종 거래일 계산용
US_HOLIDAYS = pd.DatetimeIndex(sorted(set(ASSUMED_US_HOLIDAYS) | set(pd.to_datetime(
    ["2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"]))))


def expected_last_trading_day(now_utc: Optional[pd.Timestamp] = None) -> pd.Timestamp:
    """지금 시점에서 '완결된' 마지막 정규장 거래일. 미국 동부 16:00 이전이면 전 거래일. 주말·휴장일 제외."""
    now = (now_utc or pd.Timestamp.now("UTC")).tz_convert("America/New_York")
    d = now.normalize().tz_localize(None)
    if now.hour < 16:
        d -= pd.Timedelta(days=1)
    while d.weekday() >= 5 or d in US_HOLIDAYS:
        d -= pd.Timedelta(days=1)
    return d


def on_grid(x: float, tick: float = None) -> bool:
    """4자리 반올림 후 격자 검사 — 원천의 float32 표기 잡음(167.0499945)은 무시하고, 실제 서브페니(4.255)만 격자 밖으로 본다."""
    tick = tick or TICK
    if not np.isfinite(x):
        return False
    x4 = round(float(x), 4)
    return abs(x4 / tick - round(x4 / tick)) < 1e-6


# 이 플래그가 하나라도 있으면 '확인 필요' (정상 신호로 표시하지 않음)
BLOCKING = ("NOT_AVAILABLE", "LAST_BAR_NOT_LATEST", "PRICE_JUMP", "PRICE_BELOW_1USD", "TD_IDENTITY_",
            "MISSING_DAYS", "INVALID_OHLC:", "DUPLICATE_DATES", "UNSORTED", "LAST_BAR_MAY_BE_INTRADAY")
# 표시만 (조정가·서브페니로 기준가가 0.01 격자 밖일 수 있음)
NOTE_ONLY = ("OFF_TICK_GRID",)


def load_positions(path: str = POSITIONS_CSV) -> pd.DataFrame:
    cols = ["symbol", "campaign_id", "unit_no", "fill_date", "fill_price", "shares", "trade_n", "exit_date", "exit_price", "note"]
    if not os.path.exists(path):
        return pd.DataFrame(columns=cols)
    df = pd.read_csv(path, comment="#", dtype={"symbol": str})
    if not len(df):
        return pd.DataFrame(columns=cols)
    for c in cols:
        if c not in df:
            df[c] = np.nan
    df = df.dropna(subset=["symbol", "fill_price", "shares"])
    df["fill_date"] = pd.to_datetime(df["fill_date"])
    df["exit_date"] = pd.to_datetime(df["exit_date"], errors="coerce")
    df["campaign_id"] = df["campaign_id"].fillna("").astype(str).str.strip().replace({"nan": "", "None": ""})
    df["open"] = df["exit_date"].isna() & df["exit_price"].isna()
    return df.sort_values(["symbol", "fill_date", "unit_no"])


def current_campaign(pos: pd.DataFrame) -> pd.DataFrame:
    """같은 종목의 행 중 '현재 캠페인' = 열린 유닛이 속한 campaign_id 의 모든 행(닫힌 유닛 포함 → 손절 인상 이력·최초 N 근거 보존).
    campaign_id 는 필수. 누락 행이 있거나 서로 다른 campaign_id 에 열린 유닛이 동시에 있으면 ValueError 로 거부한다(추정하지 않음)."""
    if not len(pos):
        return pos
    sym = pos["symbol"].iloc[0]
    missing = pos[pos["campaign_id"] == ""]
    if len(missing):
        raise ValueError(f"[{sym}] positions.csv 에 campaign_id 가 비어 있는 행 {len(missing)}개 — 모든 체결 행에 campaign_id 를 넣어라(예: {sym}-2026-09).")
    open_ids = sorted(pos.loc[pos["open"], "campaign_id"].unique())
    if len(open_ids) > 1:
        raise ValueError(f"[{sym}] 열린 유닛이 서로 다른 campaign_id 에 있음 {open_ids} — 한 종목에 열린 캠페인은 하나여야 한다. 입력을 확인하라.")
    if not open_ids:
        return pos.iloc[0:0]
    return pos[pos["campaign_id"] == open_ids[0]]


def load_account() -> Optional[float]:
    if not os.path.exists(ACCOUNT_JSON):
        return None
    import json
    a = json.load(open(ACCOUNT_JSON)).get("notional_A")
    return float(a) if a else None


def levels_for(sym: str, df: pd.DataFrame, flags: List[str], pos: pd.DataFrame, A: Optional[float],
               expected_last: Optional[pd.Timestamp] = None) -> Dict:
    """마지막 완결 봉 기준 다음 거래일 기준가 + 보유 유닛 관리선."""
    df = df.loc[~df.index.duplicated(keep=False)].sort_index()
    if len(df) == 0:                                   # 완결 봉 0개(오늘 상장 등) → 계산 불가, 확인 필요
        return dict(symbol=sym, as_of=None, status="확인 필요",
                    reasons=" | ".join(flags + ["NO_COMPLETED_BARS(완결 일봉 0개)"]),
                    units=int(pos["open"].sum()) if len(pos) else 0, units_bought=len(pos))
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    t = len(df) - 1
    n = n_value(h, l, c, CFG.n_period)
    as_of = df.index[t]
    row: Dict = dict(symbol=sym, as_of=str(as_of.date()), close=float(c[t]), bars=len(df))
    row["N"] = float(n[t]) if np.isfinite(n[t]) else np.nan                       # as_of 종가 반영 → 다음 거래일 판단용
    row["h55"] = float(np.max(h[-CFG.entry_lookback:])) if len(df) >= CFG.entry_lookback else np.nan
    row["l20"] = float(np.min(l[-CFG.exit_lookback:])) if len(df) >= CFG.exit_lookback else np.nan
    row["entry_level"] = row["h55"] + TICK if np.isfinite(row["h55"]) else np.nan
    row["exit_level"] = row["l20"] - TICK if np.isfinite(row["l20"]) else np.nan
    row["unit_shares"] = (int(math.floor(A * CFG.risk_per_unit / row["N"])) if (A and np.isfinite(row["N"]) and row["N"] > 0) else None)

    blocking = [f for f in flags if any(f.startswith(b) or b in f for b in BLOCKING)]
    notes = [f for f in flags if any(f.startswith(b) for b in NOTE_ONLY)]
    if expected_last is not None and as_of < expected_last:                     # 파일 전체가 함께 오래된 경우도 잡는다
        blocking.append(f"STALE_DATA:last_bar {as_of.date()} < expected {expected_last.date()}")
    if len(df) < CFG.entry_lookback or not np.isfinite(row["N"]):
        blocking.append(f"INSUFFICIENT_BARS:{len(df)} (<{CFG.entry_lookback} 또는 N 없음)")
    for name in ("entry_level", "exit_level"):                                   # 실제 사용할 기준가가 0.01 격자 밖이면 정상 안내 불가(임의 반올림 없음)
        v = row[name]
        if np.isfinite(v) and not on_grid(v):
            blocking.append(f"LEVEL_OFF_GRID:{name}={v:.4f} (틱 {TICK} 격자 밖 — D-23 판정 전 정상 안내 안 함)")
    row["status"] = "확인 필요" if blocking else "정상"
    row["reasons"] = " | ".join(blocking)
    row["notes"] = " | ".join(notes)

    # ---- 보유 유닛 (인철 입력) ----
    row.update(units=0, units_bought=0, next_add_level=np.nan, stops="", trigger="", trade_n=np.nan, trade_n_source="")
    camp = current_campaign(pos)
    if len(camp):
        tn = camp["trade_n"].dropna()
        if len(tn):
            trade_n, src = float(tn.iloc[0]), "positions.csv"
        else:                                           # [D-1 가정] 첫 체결일 전일까지의 N
            d0 = camp["fill_date"].iloc[0]
            idx = df.index.searchsorted(d0) - 1
            trade_n, src = (float(n[idx]) if idx >= 0 and np.isfinite(n[idx]) else np.nan), "assumed:N(first_fill_date-1) [D-1 미확정]"
        fills = camp["fill_price"].astype(float).tolist()
        k = len(fills)                                  # 캠페인 내 전체 매수(닫힌 유닛 포함) — 손절 인상 이력 보존
        stops_all = [f - CFG.stop_n * trade_n + CFG.pyramid_step_n * trade_n * (k - 1 - i) for i, f in enumerate(fills)]
        open_mask = camp["open"].tolist()
        open_units = [(int(u), s, sh) for u, s, sh, o in zip(camp["unit_no"], stops_all, camp["shares"], open_mask) if o]
        n_open = len(open_units)
        row.update(units=n_open, units_bought=k, trade_n=trade_n, trade_n_source=src,
                   shares_open=int(sum(sh for _, _, sh in open_units)),
                   next_add_level=(fills[-1] + CFG.pyramid_step_n * trade_n) if (n_open and n_open < CFG.max_units_per_symbol) else np.nan,
                   stops=" / ".join(f"U{u}:{s:.4f}" for u, s, _ in open_units),
                   trigger=" / ".join(f"U{u}:{max(s, row['exit_level']):.4f}({'STOP' if s >= row['exit_level'] else 'EXIT20'})" for u, s, _ in open_units))
        if n_open == 0:
            row.update(next_add_level=np.nan, stops="(캠페인 종료)", trigger="")
    return row


def main(symbols: Optional[List[str]], positions_path: str = POSITIONS_CSV, out_name: str = "levels",
         data_dir: Optional[str] = None):
    os.makedirs(OUT, exist_ok=True)
    sample = pd.read_csv(os.path.join(ROOT, "data", "sample20.csv"))["symbol"].tolist()
    pos_all = load_positions(positions_path)
    syms = symbols or sorted(set(sample) | set(pos_all["symbol"]))
    A = load_account()
    # data_dir 지정 시 그 폴더의 <SYM>.raw.csv 를 읽는다(예: 야후 일봉 data/prices_yahoo). 미지정 시 기존 경로(prices_td).
    series = {s: (load_raw(s, data_dir) if data_dir else get_series(s)) for s in syms}
    fetched = {s: v for s, v in series.items() if v.df is not None and len(v.df) > 0}
    if not fetched:
        print(f"확보된 일봉이 없습니다(요청 {len(syms)}종목). 먼저 yahoo_daily.py 로 일봉을 받고 --data-dir 를 맞게 지정하세요.")
        print(f"  data-dir: {data_dir or '(기본 prices_td)'}")
        return
    last = max(v.df.index.max() for v in fetched.values())
    expected = expected_last_trading_day()
    cal = consensus_calendar(series, last - pd.DateOffset(years=1), last)
    rows = []
    for s in syms:
        v = series[s]
        pos = pos_all[pos_all.symbol == s]
        if v.df is None or len(v.df) == 0:
            reason = (v.error or "raw 파일 없음") if v.df is None else "NO_COMPLETED_BARS(완결 일봉 0개)"
            rows.append(dict(symbol=s, as_of=None, status="확인 필요", reasons=reason,
                             units=int(pos["open"].sum()) if len(pos) else 0, units_bought=len(pos)))
            continue
        chk = check_symbol(v, cal, last - pd.DateOffset(years=1), last, None, None)
        rows.append(levels_for(s, v.df, chk.flags, pos, A, expected))
    out = pd.DataFrame(rows)
    out.to_csv(os.path.join(OUT, f"{out_name}.csv"), index=False)
    f = lambda x: "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else (f"{x:.4f}" if isinstance(x, float) else str(x))
    gi = lambda x: 0 if x is None or (isinstance(x, float) and not np.isfinite(x)) else int(x)
    with open(os.path.join(OUT, f"{out_name}.md"), "w") as fh:
        fh.write(f"# 터틀 System 2 기준가 — 일봉 기준. 데이터 마지막 봉 {last.date()} / 기대 최종 거래일 {expected.date()}"
                 f"{' (미갱신!)' if last < expected else ''}. 다음 거래일용, 실시간 아님. 생성 {pd.Timestamp.now('UTC'):%Y-%m-%d %H:%M} UTC\n")
        fh.write(f"명목계좌 A: {'미입력(수량 미계산)' if not A else f'{A:,.0f}'} · 틱 {TICK} · 보유 입력: {os.path.relpath(positions_path, ROOT)} · '확인 필요' 행은 정상 신호가 아님(사유 열)\n\n")
        fh.write("| 종목 | 상태 | 종가 | N | 진입 기준가(H55+틱) | 청산선(L20−틱) | 유닛수량(A 기준) | 보유 유닛(열림/매수) | 캠페인 N | 다음 추가매수 | 유닛별 손절 | 유닛별 청산 트리거 | 사유/주의 |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for _, r in out.iterrows():
            fh.write(f"| {r.symbol} | {r.status} | {f(r.get('close'))} | {f(r.get('N'))} | {f(r.get('entry_level'))} | {f(r.get('exit_level'))} | "
                     f"{r.get('unit_shares') if r.get('unit_shares') is not None and not (isinstance(r.get('unit_shares'), float) and np.isnan(r.get('unit_shares'))) else '—'} | "
                     f"{gi(r.get('units'))}/{gi(r.get('units_bought'))} | {f(r.get('trade_n'))}{(' ['+r.trade_n_source+']') if isinstance(r.get('trade_n_source'), str) and r.trade_n_source.startswith('assumed') else ''} | "
                     f"{f(r.get('next_add_level'))} | {r.get('stops') or '—'} | {r.get('trigger') or '—'} | {(r.get('reasons') or '')}{(' · ' + r.notes) if isinstance(r.get('notes'), str) and r.notes else ''} |\n")
        fh.write("\n## TradingView 알림 설정 — 상태 '정상' 종목만 (Essential 기술 알림, 지표 HB_T55_check 의 alertcondition 사용)\n"
                 "가격 알림을 1틱 옮기는 방식은 쓰지 않는다(수신 가격 격자 미확인). 지표가 원래 기준가에 대한 >= / <= 를 직접 계산한다.\n"
                 "만드는 법: 일봉 차트에 HB_T55_check 를 올리고 알림 만들기 → 조건 = HB_T55_check → 아래 조건명 선택, 빈도 '봉당 1회'(Once Per Bar), 만료 '없음'.\n")
        ok = [r for _, r in out.iterrows() if r.get("as_of") is not None and r.status == "정상"]
        if not ok:
            fh.write("- (정상 종목 없음 — 위 표의 사유 확인)\n")
        for r in ok:
            fh.write(f"- NASDAQ:{r.symbol} 진입: 조건 **T55 ENTRY** (지표가 계산: 고가 >= {f(r.entry_level)})\n")
            if r.get("units", 0):
                trig_max = max(float(t.split(':')[1].split('(')[0]) for t in r.trigger.split(" / "))
                fh.write(f"  - 보유 관리: 지표 입력 add_level={f(r.next_add_level)} → 조건 **ADD** / stop_level={trig_max:.4f}(열린 유닛 중 가장 높은 트리거) → 조건 **STOP**; 나머지 유닛 트리거 {r.trigger}\n")
        skipped = [r for _, r in out.iterrows() if r.get("as_of") is not None and r.status != "정상"]
        if skipped:
            fh.write("- 알림 목록에서 제외(확인 필요): " + ", ".join(r.symbol for r in skipped) + "\n")
    print(open(os.path.join(OUT, f"{out_name}.md")).read())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None, help="쉼표 구분. 기본 = 표본 20 + positions 종목")
    ap.add_argument("--symbols-file", default=None, help="한 줄 1심볼 파일(전 종목용, 예 nasdaq_common.txt)")
    ap.add_argument("--positions", default=POSITIONS_CSV, help="실제 체결 CSV (기본 live/positions.csv)")
    ap.add_argument("--out", default="levels", help="출력 파일 이름(live/out/<out>.md/.csv)")
    ap.add_argument("--data-dir", default=None, help="일봉 폴더(<SYM>.raw.csv). 예: data/prices_yahoo. 미지정 시 prices_td")
    a = ap.parse_args()
    syms = None
    if a.symbols_file:
        syms = [l.strip().upper() for l in open(a.symbols_file) if l.strip() and not l.startswith("#")]
    elif a.symbols:
        syms = a.symbols.split(",")
    main(syms, a.positions, a.out, a.data_dir)
