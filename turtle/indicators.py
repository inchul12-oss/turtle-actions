"""
지표 계산. 전부 numpy 배열 입력, 같은 길이 배열 출력(정의 안 되는 앞부분은 NaN).

핵심 원칙: 인덱스 t의 값은 t일 '장중 판단'에 쓰이므로 t일 데이터를 포함하면 안 되는 것과
포함해도 되는 것을 함수 이름으로 구분한다.
  - prior_max/prior_min : t 제외, [t-lookback, t-1] 구간        → 돌파/청산 기준선
  - n_value             : t 포함(종가 확정 후 값). 엔진은 t일 판단에 n_value[t-1]을 쓴다.
"""
import numpy as np


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """[원문 p13] TR = max(H−L, H−PDC, PDC−L). PDC = 전일 종가. 첫 봉은 PDC가 없어 NaN."""
    tr = np.full(len(high), np.nan)
    pdc = close[:-1]
    h, l = high[1:], low[1:]
    tr[1:] = np.maximum.reduce([h - l, h - pdc, pdc - l])
    return tr


def n_value(high, low, close, period: int = 20, seed: str = "sma") -> np.ndarray:
    """
    [원문 p13] N_t = ((period−1)·N_{t−1} + TR_t) / period.
    [원문 p13] 초깃값: 직전 period일 TR의 단순평균 (TR은 인덱스 1부터 존재하므로 첫 N은 인덱스 period).
    n_value[t]는 t일 종가까지 반영된 값이다.
    """
    tr = true_range(high, low, close)
    n = np.full(len(high), np.nan)
    if len(high) <= period:
        return n
    if seed != "sma":
        raise ValueError("seed must be 'sma' (원문 규정)")
    n[period] = np.nanmean(tr[1:period + 1])
    for t in range(period + 1, len(high)):
        n[t] = ((period - 1) * n[t - 1] + tr[t]) / period
    return n


def prior_max(arr: np.ndarray, lookback: int) -> np.ndarray:
    """out[t] = max(arr[t-lookback .. t-1]). 당일(t) 제외. 데이터 부족 구간은 NaN."""
    out = np.full(len(arr), np.nan)
    for t in range(lookback, len(arr)):
        out[t] = np.max(arr[t - lookback:t])
    return out


def prior_min(arr: np.ndarray, lookback: int) -> np.ndarray:
    """out[t] = min(arr[t-lookback .. t-1]). 당일(t) 제외."""
    out = np.full(len(arr), np.nan)
    for t in range(lookback, len(arr)):
        out[t] = np.min(arr[t - lookback:t])
    return out


def weekly_frozen(values: np.ndarray, dates) -> np.ndarray:
    """
    [설정 n_refresh='weekly'] 각 주의 첫 거래일 직전 값(= 그 전 주 마지막 종가 기준 값)을 그 주 내내 고정.
    values[t]는 t 종가 반영값이므로, 주의 첫 거래일 t0에는 values[t0-1]을 쓰고 주중 유지.
    """
    out = np.full(len(values), np.nan)
    dates = np.asarray(dates)
    cur = np.nan
    prev_week = None
    for t in range(len(values)):
        d = np.datetime64(dates[t], "D").astype("datetime64[D]").astype(object)
        wk = d.isocalendar()[:2]
        if wk != prev_week:
            cur = values[t - 1] if t > 0 else np.nan
            prev_week = wk
        out[t] = cur
    return out
