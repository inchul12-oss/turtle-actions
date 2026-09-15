"""
STEP 2 — 데이터 연결 점검 (판호 ■4: 마지막 봉 완결 여부 / 날짜·중복·누락 / 분할·틱 단위 / 분류·상장 상태).

원칙: 종목을 임의로 탈락시키지 않는다. 모든 표본 종목이 한 행씩 나오고, 문제는 flags 열에 사유 코드로 남는다.
탈락 판단은 판호/인철이 한다.

거래일 캘린더: Twelve Data 의 exchange_schedule(휴장일)은 유료(ultra) 엔드포인트여서 쓰지 못했다.
대신 '표본 합의 캘린더'를 쓴다 — 확보된 종목 중 절반 이상에 봉이 있는 평일 = 거래일.
합의 캘런더에 없는 평일은 '전 종목 공통 결손(휴장일 후보)'으로 따로 보고하고, 알려진 미국 휴장일 목록(가정)과 대조만 한다.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from turtle.engine import TurtleEngine, EPS                     # noqa: E402  입력 검증기 재사용
from step2.td_source import Series                              # noqa: E402

# 가정(검증 불가 — 유료 엔드포인트): 2025-09 ~ 2026-09 미국 증시 휴장일. 합의 캘린더와 대조하는 용도로만 쓴다.
ASSUMED_US_HOLIDAYS = pd.to_datetime([
    "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
])

TICK = 0.01          # TurtleConfig.tick_size 기본값. 1달러 미만 종목은 실제 호가단위가 0.0001 이라 이 가정이 깨진다 → 플래그


@dataclass
class CheckResult:
    symbol: str
    status: str                         # fetched / not_available
    flags: List[str] = field(default_factory=list)
    info: Dict = field(default_factory=dict)


def consensus_calendar(series: Dict[str, Series], start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """확보된 종목 중 절반 이상에 봉이 있는 날 = 거래일. (주말은 애초에 봉이 없으므로 자연히 빠진다)"""
    counts: Dict[pd.Timestamp, int] = {}
    n = 0
    for s in series.values():
        if s.df is None:
            continue
        n += 1
        for d in s.df.index[(s.df.index >= start) & (s.df.index <= end)].unique():
            counts[d] = counts.get(d, 0) + 1
    days = sorted(d for d, c in counts.items() if c * 2 >= n)
    return pd.DatetimeIndex(days)


def weekday_gaps(calendar: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """합의 캘린더에 없는 평일 = 전 종목 공통 결손(휴장일 후보)."""
    bdays = pd.bdate_range(start, end)
    return bdays.difference(calendar)


def check_symbol(s: Series, calendar: pd.DatetimeIndex, window_start: pd.Timestamp, expected_last: pd.Timestamp,
                 listing_row: Optional[dict], td_identity: Optional[str]) -> CheckResult:
    r = CheckResult(s.symbol, "fetched" if s.df is not None else "not_available")
    r.info["source"] = s.source
    r.info["fetched_utc"] = s.fetched_utc
    r.info["av_listing"] = f"{listing_row['name']} / {listing_row['assetType']} / {listing_row['status']}" if listing_row else "not in AV active list"
    r.info["td_identity"] = td_identity or "(미조회)"
    if s.df is None:
        r.flags.append(f"NOT_AVAILABLE_ON_TD:{s.error}")
        return r
    df = s.df
    r.info["rows_total"] = int(len(df))
    r.info["first_bar"] = str(df.index.min().date())
    r.info["last_bar"] = str(df.index.max().date())

    # 1) 마지막 봉 완결 여부: 마지막 봉 날짜가 기대 최종 거래일과 같고, 수집 시각이 그 날 장 마감(20:00 UTC) 이후
    last = df.index.max()
    if last != expected_last:
        r.flags.append(f"LAST_BAR_NOT_LATEST:{last.date()}≠{expected_last.date()}")
    fetched = pd.Timestamp(s.fetched_utc) if s.fetched_utc else None
    if fetched is not None and fetched < last + pd.Timedelta(hours=20):
        r.flags.append("LAST_BAR_MAY_BE_INTRADAY(fetched before 20:00 UTC of last bar)")
    r.info["last_bar_completed"] = (last == expected_last) and (fetched is not None and fetched >= last + pd.Timedelta(hours=20))

    # 2) 날짜·중복·누락 (1년 창)
    w = df[(df.index >= window_start) & (df.index <= expected_last)]
    r.info["rows_window"] = int(len(w))
    dup = w.index[w.index.duplicated()]
    if len(dup):
        r.flags.append(f"DUPLICATE_DATES:{[str(d.date()) for d in dup[:5]]}")
    if not w.index.is_monotonic_increasing:
        r.flags.append("UNSORTED")
    present = w.index.unique()
    cal_in_range = calendar[(calendar >= max(window_start, df.index.min())) & (calendar <= min(expected_last, last))]
    missing = cal_in_range.difference(present)
    r.info["missing_vs_consensus"] = [str(d.date()) for d in missing]
    if len(missing):
        r.flags.append(f"MISSING_DAYS:{len(missing)}:{[str(d.date()) for d in missing[:6]]}")
    extra = present.difference(calendar)
    if len(extra):
        r.flags.append(f"BARS_ON_NON_CONSENSUS_DAYS:{[str(d.date()) for d in extra[:5]]}")
    if df.index.min() > window_start:
        r.flags.append(f"HISTORY_STARTS_LATE:{df.index.min().date()}")

    # 3) OHLC 유효성(엔진 검증기 그대로) + 거래 정지 흔적
    try:
        TurtleEngine._validate_index(s.symbol, w.loc[~w.index.duplicated()])
        TurtleEngine._validate_ohlc(s.symbol, w)
        r.info["ohlc_valid"] = True
    except ValueError as e:
        r.info["ohlc_valid"] = False
        r.flags.append(f"INVALID_OHLC:{str(e)[:120]}")
    try:                                                   # 전체 기간도 참고로(2010~) — 엔진에 전체를 넣을 때 걸리는 봉
        TurtleEngine._validate_ohlc(s.symbol, df)
    except ValueError as e:
        r.flags.append(f"INVALID_OHLC_FULL_HISTORY:{str(e)[:120]}")
    zero_vol = int((w["volume"] <= 0).sum())
    flat = int(((w["open"] == w["high"]) & (w["high"] == w["low"]) & (w["low"] == w["close"])).sum())
    r.info["zero_volume_bars"] = zero_vol
    r.info["flat_bars"] = flat
    if zero_vol:
        r.flags.append(f"ZERO_VOLUME_BARS:{zero_vol}")
    if flat:
        r.flags.append(f"FLAT_OHLC_BARS:{flat}")
    med_vol = float(w["volume"].median()) if len(w) else float("nan")
    r.info["median_volume"] = med_vol
    if med_vol < 1000:
        r.flags.append(f"THIN_VOLUME(median {med_vol:.0f})")

    # 4) 분할 전후 가격·틱 단위
    c = w["close"].to_numpy(float)
    if len(c) > 1:
        ratio = c[1:] / c[:-1]
        jumps = np.where((ratio > 2.5) | (ratio < 0.4))[0]
        r.info["price_jumps"] = [(str(w.index[i + 1].date()), round(float(ratio[i]), 3)) for i in jumps]
        if len(jumps):
            r.flags.append(f"PRICE_JUMP(possible unadjusted split):{r.info['price_jumps'][:3]}")
    sub_tick = 0
    for col in ("open", "high", "low", "close"):
        v = np.round(w[col].to_numpy(float), 4)          # TD 는 float32 표기(181.71001)를 내보내므로 4자리로 반올림 후 격자 검사
        sub_tick += int((np.abs(v / TICK - np.round(v / TICK)) > 1e-6).sum())
    r.info["prices_off_tick_grid"] = sub_tick
    if sub_tick:
        r.flags.append(f"OFF_TICK_GRID(0.01):{sub_tick} values(분할 소급조정가 또는 서브페니 체결 — 기준가 '+1틱' 이 실제 호가 격자와 어긋남)")
    if len(c) and float(np.nanmin(w["low"])) < 1.0:
        r.flags.append("PRICE_BELOW_1USD(호가단위 0.0001 구간 — tick_size=0.01 가정 불일치)")

    # 5) 분류·상장 상태
    if td_identity and ("ETF" in td_identity or "Fund" in td_identity):
        r.flags.append(f"TD_IDENTITY_NOT_COMMON_STOCK:{td_identity}")
    if td_identity and "ambiguous" in td_identity.lower():
        r.flags.append(f"TD_IDENTITY_AMBIGUOUS:{td_identity}")
    return r


def results_frame(results: List[CheckResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        rows.append(dict(symbol=r.symbol, status=r.status,
                         rows_window=r.info.get("rows_window"), first_bar=r.info.get("first_bar"), last_bar=r.info.get("last_bar"),
                         last_bar_completed=r.info.get("last_bar_completed"),
                         missing_days=len(r.info.get("missing_vs_consensus", [])),
                         ohlc_valid=r.info.get("ohlc_valid"), off_tick=r.info.get("prices_off_tick_grid"),
                         av_listing=r.info.get("av_listing"), td_identity=r.info.get("td_identity"),
                         n_flags=len(r.flags), flag_text=" | ".join(r.flags)))
    return pd.DataFrame(rows)
