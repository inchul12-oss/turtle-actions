"""
STEP 2 — Twelve Data 일봉 연결 (데이터 출처 계층).

두 경로를 지원한다.
  (A) raw 파일 경로: 이번 세션에서 Twelve Data MCP(get_time_series) 응답을 그대로 저장한
      data/prices_td/<SYM>.raw.csv ("datetime;open;high;low;close;volume", 최신순) + <SYM>.meta.json
  (B) REST 경로: TWELVEDATA_API_KEY 환경변수가 있으면 https://api.twelvedata.com/time_series 를 직접 호출.
      → 이번 세션 샌드박스는 외부 인터넷이 막혀 있어 (B)는 실행하지 못했다. 코드만 제공하며 검증되지 않은 경로다.

정규화 결과: DatetimeIndex 오름차순, 열 open/high/low/close/volume(float). 정렬만 하고 값은 손대지 않는다
(중복·결손·비정상 봉은 check_connection.py 가 보고하고, 엔진 검증기가 거부한다).
"""
from __future__ import annotations

import io
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.normpath(os.path.join(HERE, "..", "data", "prices_td"))
TD_BASE = "https://api.twelvedata.com/time_series"


@dataclass
class Series:
    symbol: str
    df: Optional[pd.DataFrame]          # None = 확보 실패
    source: str
    fetched_utc: str
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    meta: Dict = field(default_factory=dict)


def parse_td_csv(text: str) -> pd.DataFrame:
    """Twelve Data CSV(';' 구분, 최신순) → 오름차순 DataFrame. 값은 변경하지 않는다."""
    df = pd.read_csv(io.StringIO(text), sep=";")
    df.columns = [c.strip().lower() for c in df.columns]
    if "datetime" not in df.columns:
        raise ValueError(f"예상 밖 형식: columns={list(df.columns)}")
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_index()          # 명시적 정렬(데이터 단계). 중복은 남겨 두고 검사 단계에서 보고


def load_raw(symbol: str, raw_dir: str = RAW_DIR) -> Series:
    p = os.path.join(raw_dir, f"{symbol}.raw.csv")
    m = os.path.join(raw_dir, f"{symbol}.meta.json")
    meta = json.load(open(m)) if os.path.exists(m) else {}
    if not os.path.exists(p):
        return Series(symbol, None, "Twelve Data (MCP get_time_series)", meta.get("fetched_utc", ""),
                      error=meta.get("error", "raw 파일 없음"), meta=meta)
    text = open(p).read()
    if text.startswith("Error") or "datetime" not in text.splitlines()[0]:
        return Series(symbol, None, meta.get("source", "Twelve Data"), meta.get("fetched_utc", ""),
                      error=text.strip()[:200], meta=meta)
    df = parse_td_csv(text)
    return Series(symbol, df, meta.get("source", "Twelve Data (MCP get_time_series)"),
                  meta.get("fetched_utc", ""), meta=meta)


def fetch_rest(symbol: str, api_key: str, start_date: str = "2010-01-01", end_date: Optional[str] = None,
               exchange: str = "NASDAQ", outputsize: int = 5000, timeout: int = 30) -> Series:
    """
    (B) REST 직접 호출. 1회 = 1 크레딧(Basic 무료 플랜: 800 크레딧/일, 분당 8회 — 공식 가격표 기준, 이번 세션에서 get_api_usage 로 plan_limit=800 확인).
    주의: MCP와 동일하게 start/end 만 주면 outputsize 기본 30 이 우선하므로 outputsize 를 반드시 넘긴다.
    이 함수는 이번 세션에서 실행·검증되지 않았다(샌드박스 인터넷 차단).
    """
    q = dict(symbol=symbol, interval="1day", exchange=exchange, outputsize=outputsize, format="CSV",
             delimiter=";", start_date=start_date, apikey=api_key)
    if end_date:
        q["end_date"] = end_date
    url = TD_BASE + "?" + urllib.parse.urlencode(q)
    fetched = pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            text = r.read().decode("utf-8")
    except Exception as e:                      # 네트워크/HTTP 오류는 그대로 보고
        return Series(symbol, None, "Twelve Data REST /time_series", fetched, error=f"{type(e).__name__}: {e}")
    if text.lstrip().startswith("{"):           # 오류 응답은 JSON
        return Series(symbol, None, "Twelve Data REST /time_series", fetched, error=text.strip()[:300])
    return Series(symbol, parse_td_csv(text), "Twelve Data REST /time_series", fetched)


def get_series(symbol: str, prefer_rest: bool = False) -> Series:
    key = os.environ.get("TWELVEDATA_API_KEY")
    if prefer_rest and key:
        return fetch_rest(symbol, key)
    return load_raw(symbol)
