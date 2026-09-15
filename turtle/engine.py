"""
터틀 System 2 (55일 돌파) 주식 매수 전용 규칙 엔진 + 일봉 시뮬레이터.  (STEP 1.1)

설계 원칙
- 판단은 '그 시점에 알 수 있는 값'만 사용: t일 장중 판단에는 N[t-1], 직전 55일 고가(t 제외), 강도 순위는 t-1 종가까지.
- 하루는 세그먼트로 나눠 처리한다 (RULES §9):
    A. 시가에서 이미 확정되는 체결 (시가가 손절/이탈가 아래 → 시가 청산, 시가가 돌파가/추가매수가 위 → 시가 매수)
    B/C. 정책(config.intraday_order)에 따라 '고가 구간 매수'와 '저가 구간 매도'의 순서를 정함
         designated : 매도(L) → 매수(H, 오늘 매도 없는 종목만) → 매수 후 인상된 손절 확인(SAMEDAY)
         OHLC       : 매수(H) → 매도(L)
         OLHC       : 매도(L) → 매수(H)
    D. 배당, 상장폐지, 평가, 명목계좌 단계 갱신(다음 날부터 반영)
- 같은 날 매수 조건과 매도 조건이 둘 다 참인 종목-일은 이벤트에 ambiguous=True 를 남긴다.
  어떤 정책도 '모든 경로의 상한/하한'이 아니다. 지정 처리 정책일 뿐이다.
- 원문에 없는 가정은 전부 TurtleConfig 설정값이며 RULES.md의 D-번호와 대응한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .config import TurtleConfig
from .indicators import n_value, prior_max, prior_min, weekly_frozen

EPS = 1e-9   # 부동소수 비교 허용오차 (28.3 + 0.6 = 28.900000000000002 같은 경우가 '미달'로 판정되지 않게)


def ge(a, b) -> bool:
    return a >= b - EPS


def le(a, b) -> bool:
    return a <= b + EPS


# --------------------------------------------------------------------------- 데이터 컨테이너
@dataclass
class SymbolData:
    """한 종목의 일봉. 가격은 분할 조정 완료 상태여야 한다(data.split_adjust 참고)."""
    symbol: str
    df: pd.DataFrame                      # index: DatetimeIndex, cols: open high low close (volume 선택)
    dividends: Dict[pd.Timestamp, float] = field(default_factory=dict)  # 배당락일 -> 주당 현금(조정 후)
    delisted: bool = False                # True = 상장목록 자료로 상폐가 확인된 종목 (마지막 봉 = 상폐 전 마지막 거래일)
    delisting_date: Optional[pd.Timestamp] = None   # 자료상 상폐일. None이면 '가격 데이터가 끊긴 날'일 뿐 상폐일로 간주하지 않음 [D-14]
    terminal_action: Optional[dict] = None
    # 최종 기업행사 자료 [D-14]. 예: {"type":"cash","per_share":100.0,"pay_date":Timestamp}  (현금 인수: 대가는 pay_date에 현금화)
    #                          {"type":"stock", ...} / {"type":"mixed", ...} → 현재 엔진은 모델링 불가 → 미확인으로 기록
    # None → '최종 회수금액 미확인'. 기본 처리는 미확인 버킷으로 이동(현금화하지 않음)
    issuer: Optional[str] = None          # 발행사 id (클래스주 합산용). None이면 심볼 자체
    issuer_confirmed: bool = False        # True = 확인된 식별자(CIK 등) / False = 이름에서 추정한 키 [D-15]
    group_close: Optional[str] = None     # 밀접 상관군 id (없으면 한도 미적용)
    group_loose: Optional[str] = None


@dataclass
class Unit:
    unit_no: int
    entry_date: pd.Timestamp
    fill: float
    shares: int
    stop: float
    intended_level: float   # 갭 없이 체결됐어야 할 가격(돌파가 또는 추가매수 기준가)
    gapped: bool


@dataclass
class Position:
    symbol: str
    trade_n: float          # 캠페인 N: 최초 진입 시점 N_used. 피라미딩 간격·손절폭에 고정 [원문 p20,p23][D-1]
    unit_shares: int        # 캠페인 목표 유닛 수량: 최초 진입 시점에 고정 [D-1 — 구현 가정, 원형 미승인(판호 1.2 재검토). 재현용 보존]
    units: List[Unit] = field(default_factory=list)
    next_add_level: float = np.nan
    pending_terminal: bool = False   # [R4] 가격 관측은 끝났고 기업행사 효력일 대기 중

    @property
    def shares(self) -> int:
        return sum(u.shares for u in self.units)


# --------------------------------------------------------------------------- 엔진
class TurtleEngine:
    NOTIONAL_RULE_FLOOR = 0.5   # 축소 임계 T(k)=0.5A(1−0.8^k) 가 수렴하는 지점. NAV ≤ 0.5A 는 원문 미규정 → 미지원 종료 [D-21]
    def __init__(self, data: Dict[str, SymbolData], cfg: Optional[TurtleConfig] = None):
        self.cfg = cfg or TurtleConfig()
        self.data = data
        self.cash = self.cfg.start_equity
        self.equity = self.cfg.start_equity
        self.year_base = self.cfg.start_equity     # A: 연초 기준금액 [D-3]
        self.dd_step = 0                            # 명목계좌 축소 단계 k (명목 = A × 0.8^k) [원문 p18]
        self.positions: Dict[str, Position] = {}
        self.events: List[dict] = []
        self.equity_curve: List[tuple] = []
        self.last_close: Dict[str, float] = {}
        self._skip_seen = set()
        self._exited_today = set()
        self._bought_today: Dict[str, List[dict]] = {}
        self.receivables: List[dict] = []      # 확정됐지만 아직 지급되지 않은 대가 [D-14]: {pay_date, amount, symbol}
        self.unresolved: List[dict] = []       # 최종 회수금액 미확인 포지션 [D-14]: 현금화하지 않고 별도 보관
        self.halt: Optional[dict] = None       # 평가 중단 상태(체크포인트). None이면 정상 진행
        self._ran = False
        self.last_processed_date = None
        self.requested_start = self.requested_end = self.data_start = self.data_end = None
        self._prep()
        if self.cfg.stop_policy in self.cfg.KNOWN_DEFECT_POLICIES:
            self._log(date=None, symbol=None, event="WARNING_KNOWN_DEFECT_POLICY", reason=self.cfg.stop_policy,
                      note="재현 전용 결함 정책으로 실행됨. 기준 실행 결과로 사용 금지")

    # ----------------------------------------------------------------- 지표 사전계산
    def _prep(self):
        c = self.cfg
        self.ind = {}
        for sym, sd in self.data.items():
            df = sd.df
            self._validate_index(sym, df)
            self._validate_ohlc(sym, df)
            h, l, cl = df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
            n = n_value(h, l, cl, c.n_period, c.n_seed)
            if c.n_refresh == "weekly":
                n_used = weekly_frozen(n, df.index.values)
            elif c.n_refresh == "daily":
                n_used = np.roll(n, 1); n_used[0] = np.nan        # t일 판단에는 N[t-1]
            else:
                raise ValueError(c.n_refresh)
            self.ind[sym] = dict(
                n=n, n_used=n_used, close=cl,
                hi_entry=prior_max(h, c.entry_lookback),          # t 제외 직전 55일 고가
                lo_exit=prior_min(l, c.exit_lookback),            # t 제외 직전 20일 저가
                pos={d: i for i, d in enumerate(df.index)},
            )

    @staticmethod
    def _validate_index(sym: str, df: pd.DataFrame):
        """
        [판호 1.4 R5] 지표는 행 순서대로 계산되므로 날짜 인덱스가 DatetimeIndex · 오름차순 · 유일 이어야 한다.
        비정렬/중복은 임의 정렬·선택하지 않고 ValueError로 거부한다 (정렬은 data.from_ohlc_frame 등 데이터 단계에서 명시적으로).
        """
        idx = df.index
        if not isinstance(idx, pd.DatetimeIndex):
            raise ValueError(f"[{sym}] 인덱스가 DatetimeIndex가 아님({type(idx).__name__}). 날짜 인덱스로 변환해 입력하라.")
        if idx.hasnans:
            raise ValueError(f"[{sym}] 인덱스에 NaT가 있음.")
        if not idx.is_monotonic_increasing:
            bad = idx[1:][idx[1:] < idx[:-1]][:3]
            raise ValueError(f"[{sym}] 날짜 인덱스가 오름차순이 아님(예: {[str(d.date()) for d in bad]}). "
                             f"엔진은 정렬하지 않는다 — 데이터 단계에서 명시적으로 정렬하라.")
        if not idx.is_unique:
            dup = idx[idx.duplicated()][:3]
            raise ValueError(f"[{sym}] 중복 날짜(예: {[str(d.date()) for d in dup]}). 임의 선택하지 않는다.")

    @staticmethod
    def _validate_ohlc(sym: str, df: pd.DataFrame):
        """
        입력 단계 검증 [판호 1.3 ■1]: 일봉은 L ≤ min(O,C) ≤ max(O,C) ≤ H, 전부 유한·양수여야 한다.
        위반 봉은 임의 보정하지 않고 ValueError로 종목·날짜를 알린다.
        """
        o, h, l, cl = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        bad = ~(np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(cl)) | (l <= 0) \
              | (l > np.minimum(o, cl) + EPS) | (np.maximum(o, cl) > h + EPS)
        if bad.any():
            rows = df.index[bad][:5]
            raise ValueError(f"[{sym}] 유효하지 않은 OHLC 봉 {int(bad.sum())}개 (L ≤ min(O,C) ≤ max(O,C) ≤ H 위반 또는 비유한/비양수). "
                             f"예: {[str(d.date()) for d in rows]}. 입력 데이터를 확인하라 — 엔진은 보정하지 않는다.")

    # ----------------------------------------------------------------- 로그
    def _log(self, **kw) -> Optional[dict]:
        kw.setdefault("ambiguous", False)
        kw["cash_after"] = round(self.cash, 2)
        if kw.get("event") == "SKIP":
            key = (kw.get("symbol"), kw.get("reason"), round(kw.get("level", 0) or 0, 2))
            if key in self._skip_seen:          # 같은 종목·사유·기준가의 SKIP은 1회만 기록
                return None
            self._skip_seen.add(key)
        self.events.append(kw)
        return kw

    # ----------------------------------------------------------------- 명목계좌 / 수량
    def _notional(self) -> float:
        """[원문 p15, p18][D-3] 수량 계산에 쓰는 명목계좌. 낙폭 단계 k는 전일 종가로 확정된 값(다음 날부터 반영)."""
        c = self.cfg
        if c.equity_basis == "fixed":
            base = c.start_equity
        elif c.equity_basis == "nav":
            base = self.equity
        elif c.equity_basis == "year_base":
            base = self.year_base
        else:
            raise ValueError(c.equity_basis)
        if c.notional_drawdown_rule:
            base *= 0.8 ** self.dd_step
        return base

    def _update_drawdown_step(self):
        """
        [원문 p18][D-3, D-4] 종가 NAV로 판단. A 대비 손실이 0.10A / 0.18A / 0.244A … 에 도달하면 k=1/2/3 …
        누적 임계 T(k) = 0.5A × (1 − 0.8^k) 는 0.5A 에 수렴한다 → 손실 ≥ 0.5A 는 유한한 k로 표현되지 않는다.
        이 구간은 원문이 규정하지 않으므로 **임의의 바닥 명목으로 거래를 계속하지 않고** 미지원 종료(halt)한다 [D-21].
        NAV ≥ A 이면 해제(k=0) [원문 p18 "until such time as we reached the yearly starting equity"].
        """
        if not self.cfg.notional_drawdown_rule:      # [R2] 규칙이 꺼져 있으면 k 갱신도, 미규정 구간 HALT도 없다
            self.dd_step = 0
            return
        A = self.year_base
        if self.equity >= A:
            self.dd_step = 0
            return
        if self.equity <= self.NOTIONAL_RULE_FLOOR * A + EPS:
            self._set_halt("NOTIONAL_RULE_UNDEFINED",
                           f"NAV {self.equity:,.2f} ≤ 0.5×A({A:,.2f}): 축소 규칙의 유한 단계로 표현 불가(원문 미규정 구간)")
            return
        k, th = 0, A
        for _ in range(10_000):                       # 유한 보장: equity > 0.5A 이면 반드시 종료
            th -= 0.1 * A * (0.8 ** k)
            if self.equity <= th + EPS:
                k += 1
            else:
                break
        else:
            raise RuntimeError("drawdown step loop did not terminate")   # 도달 불가(방어)
        self.dd_step = max(self.dd_step, k)

    def _set_halt(self, reason: str, detail: str):
        """평가 중단 + 체크포인트. 매매 규칙이 아니다 — 자료 부재/미규정 구간의 미지원 상태 기록."""
        if self.halt is not None:
            return
        self.halt = dict(reason=reason, detail=detail, date=None)
        self._log(date=None, symbol=None, event="HALT", reason=reason, note=detail, segment="D")

    def _unit_shares(self, n: float) -> int:
        """[원문 p15] Unit = 1% × 명목계좌 / (N × $1). 정수 주식으로 내림 [원문 p16]."""
        if not (n > 0):
            return 0
        return int(math.floor(self._notional() * self.cfg.risk_per_unit / n))

    # ----------------------------------------------------------------- 한도
    def _total_units(self) -> int:
        return sum(len(p.units) for p in self.positions.values())

    def _issuer(self, sym: str) -> str:
        return self.data[sym].issuer or sym

    def _issuer_units(self, sym: str) -> int:
        iss = self._issuer(sym)
        return sum(len(p.units) for s, p in self.positions.items() if self._issuer(s) == iss)

    def _group_units(self, attr: str, gid: Optional[str]) -> int:
        if gid is None:
            return 0
        return sum(len(p.units) for s, p in self.positions.items() if getattr(self.data[s], attr) == gid)

    def _limit_ok(self, sym: str) -> Optional[str]:
        """[원문 p17] 보유 한도. 위반 사유 문자열 또는 None. 종목 4유닛은 발행사 단위로 합산 [D-15]."""
        c, sd = self.cfg, self.data[sym]
        if self._issuer_units(sym) >= c.max_units_per_symbol:
            if self._issuer(sym) == sym:
                return "LIMIT_SYMBOL_4U"
            src = "confirmed" if sd.issuer_confirmed else "name_inferred"
            return f"LIMIT_ISSUER_4U[{self._issuer(sym)}:{src}]"
        if self._total_units() >= c.max_units_total:
            return "LIMIT_TOTAL_12U"
        if sd.group_close and self._group_units("group_close", sd.group_close) >= c.max_units_close_corr:
            return "LIMIT_CLOSE_CORR_6U"
        if sd.group_loose and self._group_units("group_loose", sd.group_loose) >= c.max_units_loose_corr:
            return "LIMIT_LOOSE_CORR_10U"
        return None

    def limits_status(self) -> dict:
        """이번 실행에서 실제로 작동하는 한도. 상관군 매핑이 없으면 6/10유닛 한도는 '미적용'으로 표시해야 한다."""
        mapped = [sd for s, sd in self.data.items() if sd.issuer and sd.issuer != s]
        return dict(
            symbol_4u=True, total_12u=True,
            issuer_mapping=bool(mapped),
            issuer_source=("none" if not mapped else "confirmed" if all(sd.issuer_confirmed for sd in mapped)
                           else "name_inferred" if not any(sd.issuer_confirmed for sd in mapped) else "mixed"),
            close_corr_6u=any(sd.group_close for sd in self.data.values()),
            loose_corr_10u=any(sd.group_loose for sd in self.data.values()),
        )

    def _affordable(self, shares: int, price: float) -> int:
        cost_per = price * (1 + self.cfg.commission_buy)
        can = int(math.floor(self.cash / cost_per)) if cost_per > 0 else 0
        if can >= shares:
            return shares
        return can if self.cfg.partial_unit == "partial" else 0

    # ----------------------------------------------------------------- 체결
    def _buy(self, sym, date, level, fill_raw, n_used, reason, segment) -> Optional[Unit]:
        """level(돌파가/추가매수 기준가)에 매수. fill_raw = 기준가 또는 시가(갭). 한도·현금 확인."""
        c = self.cfg
        why = self._limit_ok(sym)
        if why:
            self._log(date=date, symbol=sym, event="SKIP", reason=why, level=level, segment=segment)
            return None
        gapped = fill_raw > level + EPS
        fill = fill_raw + c.slippage()
        pos = self.positions.get(sym)
        if pos is not None and c.campaign_fixed_n_and_size:
            want = pos.unit_shares                                  # [D-1] 캠페인 고정 수량
        else:
            want = self._unit_shares(n_used)
        if want < c.min_shares:
            self._log(date=date, symbol=sym, event="SKIP", reason="UNIT_SIZE_ZERO", n=n_used, level=level, segment=segment)
            return None
        got = self._affordable(want, fill)
        if got < c.min_shares:
            self._log(date=date, symbol=sym, event="SKIP", reason="INSUFFICIENT_CASH", level=level, segment=segment,
                      wanted_shares=want, cash=round(self.cash, 2), price=fill)
            return None
        if pos is None:
            pos = Position(symbol=sym, trade_n=n_used, unit_shares=want)
            self.positions[sym] = pos
        tn = pos.trade_n
        unit = Unit(unit_no=len(pos.units) + 1, entry_date=date, fill=fill, shares=got,
                    stop=fill - c.stop_n * tn, intended_level=level, gapped=gapped)
        # [원문 p23] 기존 유닛 손절 인상
        if c.stop_policy == "raise_half_n":                 # 각 유닛 손절 += ½N  ("raised by ½N")
            for u in pos.units:
                u.stop += c.pyramid_step_n * tn
        elif c.stop_policy == "last_unit_minus_2n":         # 구버전: 새 유닛 의도 기준가 − 2N (중간 갭에서 원문과 다름)
            for u in pos.units:
                u.stop = max(u.stop, level - c.stop_n * tn)
        else:
            raise ValueError(c.stop_policy)
        pos.units.append(unit)
        pos.next_add_level = fill + c.pyramid_step_n * tn   # [원문 p20] 실제 체결가 + ½N
        self.cash -= got * fill * (1 + c.commission_buy)
        ev = self._log(date=date, symbol=sym, event=reason, unit=unit.unit_no, price=round(fill, 6), shares=got,
                       level=round(level, 6), gapped=gapped, segment=segment, n_used=round(n_used, 6),
                       trade_n=round(tn, 6), stop=round(unit.stop, 6), next_add=round(pos.next_add_level, 6),
                       partial=(got < want), wanted_shares=want, stops_all=[round(u.stop, 6) for u in pos.units])
        self._bought_today.setdefault(sym, []).append(ev)
        return unit

    def _sell(self, sym, date, unit: Unit, fill_raw: float, reason: str, segment: str, ambiguous=False):
        c = self.cfg
        pos = self.positions[sym]
        fill = fill_raw - c.slippage()
        self.cash += unit.shares * fill * (1 - c.commission_sell)
        pnl = (fill - unit.fill) * unit.shares - unit.shares * (unit.fill * c.commission_buy + fill * c.commission_sell)
        self._log(date=date, symbol=sym, event=reason, unit=unit.unit_no, price=round(fill, 6), shares=unit.shares,
                  entry_price=round(unit.fill, 6), stop=round(unit.stop, 6), segment=segment, pnl=round(pnl, 2),
                  ambiguous=ambiguous, units_left=len(pos.units) - 1)
        pos.units.remove(unit)
        self._exited_today.add(sym)
        if not pos.units:
            del self.positions[sym]
            self._skip_seen = {k for k in self._skip_seen if k[0] != sym}

    # ----------------------------------------------------------------- 트리거
    def _exit_level(self, sym, t) -> float:
        lo = self.ind[sym]["lo_exit"][t]
        return lo - self.cfg.tick_size if not np.isnan(lo) else -np.inf

    def _trig(self, u: Unit, exit_lvl: float):
        """유닛별 청산 트리거 = max(손절, 20일 이탈가). 가격이 위에서 내려오므로 높은 선이 먼저 닿는다."""
        return (u.stop, "STOP_2N") if u.stop >= exit_lvl else (exit_lvl, "EXIT_20D_LOW")

    def _strength(self, sym, t, n_used) -> float:
        """[원문 p30][D-9] (C_{t-1} − C_{t-1-63}) / N_used. 전일까지의 자료만 사용. 부족하면 -inf(후순위)."""
        cl, k = self.ind[sym]["close"], self.cfg.rank_lookback
        if t - 1 - k < 0 or not (n_used > 0):
            return -np.inf
        return (cl[t - 1] - cl[t - 1 - k]) / n_used

    def _rank(self, items):
        """items: list of (sym, t, n_used) → 정렬. rank_by='symbol'이면 알파벳순(민감도용)."""
        if self.cfg.rank_by == "symbol":
            return sorted(items, key=lambda x: x[0])
        return sorted(items, key=lambda x: (-self._strength(x[0], x[1], x[2]), x[0]))

    # ----------------------------------------------------------------- 세그먼트
    def _day_info(self, sym, t) -> dict:
        df, ind = self.data[sym].df, self.ind[sym]
        o, h, l, cl = df["open"].iat[t], df["high"].iat[t], df["low"].iat[t], df["close"].iat[t]
        hi = ind["hi_entry"][t]
        return dict(t=t, o=o, h=h, l=l, c=cl, n_used=ind["n_used"][t],
                    brk=(hi + self.cfg.tick_size) if not np.isnan(hi) else np.nan,
                    exit_lvl=self._exit_level(sym, t))

    def _sell_hits(self, sym, info, price_test) -> list:
        """price_test(trig) True인 유닛 목록 [(unit, trig, reason)]"""
        pos = self.positions.get(sym)
        if not pos:
            return []
        out = []
        for u in list(pos.units):
            trig, why = self._trig(u, info["exit_lvl"])
            if price_test(trig):
                out.append((u, trig, why))
        return out

    def _chain_adds(self, sym, date, info, price_cap: float, at_open: bool, segment: str):
        """
        [원문 p20-21] price_cap(시가 또는 구간 끝 가격)이 next_add_level 이상인 동안 유닛 추가, 종목당 4유닛.
        시가 매수(at_open)는 시가 체결(갭), 구간 매수는 기준가 체결.
        """
        c = self.cfg
        pos = self.positions.get(sym)
        while pos and len(pos.units) < c.max_units_per_symbol and ge(price_cap, pos.next_add_level):
            fill_raw = info["o"] if at_open else pos.next_add_level
            u = self._buy(sym, date, pos.next_add_level, fill_raw, info["n_used"], "ADD", segment)
            if u is None or not c.allow_multi_add_per_day:
                break
            pos = self.positions.get(sym)

    # ------------------------------------------------------------ 세그먼트 A: 시가 확정 체결 (모든 종목 동시, 정책 공통)
    def _segment_open(self, date, day: Dict[str, dict]):
        """
        A. 시가에서 이미 확정되는 체결. 전 종목의 시가 청산(갭다운)을 먼저, 그 다음 전 종목의 시가 매수(갭업).
        [D-10] 시가는 전 종목 동시 시점이므로 '시가 청산 대금 → 시가 매수' 사용은 지정 가정이다.
        장중(구간) 매도 대금은 이 세그먼트에 절대 들어오지 않는다(시간순 보장).
        """
        for sym, info in day.items():
            for u, trig, why in self._sell_hits(sym, info, lambda trig: le(info["o"], trig)):
                self._sell(sym, date, u, info["o"], why, "A_open")
        adds, entries = [], []
        for sym, info in day.items():
            if not (info["n_used"] > 0):
                continue
            pos = self.positions.get(sym)
            if pos and len(pos.units) < self.cfg.max_units_per_symbol and ge(info["o"], pos.next_add_level):
                adds.append((sym, info["t"], info["n_used"]))
            elif pos is None and sym not in self._exited_today and not np.isnan(info["brk"]) and ge(info["o"], info["brk"]):
                entries.append((sym, info["t"], info["n_used"]))
        for sym, t, n_used in self._rank(adds):
            self._chain_adds(sym, date, day[sym], price_cap=day[sym]["o"], at_open=True, segment="A_open")
        for sym, t, n_used in self._rank(entries):
            info = day[sym]
            u = self._buy(sym, date, info["brk"], info["o"], n_used, "ENTRY", "A_open")
            if u is not None:
                self._chain_adds(sym, date, info, price_cap=info["o"], at_open=True, segment="A_open")

    def _buy_reach(self, sym, info) -> bool:
        """오늘 고가가 (매수 전 상태 기준) 매수 기준가에 닿았는가 — 순서 불명 표시용."""
        pos = self.positions.get(sym)
        if pos:
            return len(pos.units) < self.cfg.max_units_per_symbol and ge(info["h"], pos.next_add_level)
        return (not np.isnan(info["brk"])) and ge(info["h"], info["brk"])

    # ------------------------------------------------------------ 경로 세그먼트 (전 종목을 같은 구간으로 맞춰 전역 처리)
    def _seg_down(self, date, day, a: str, b: str, reason_suffix: str = ""):
        """
        하락 구간 a→b (예: O→L, H→L, H→C). 전 종목에 대해: 그 시점에 활성인 유닛 중 trig ∈ [P_b, P_a] 인 유닛을
        trig 가격에 청산(위에서 내려오므로 높은 trig 부터 통과하지만, 체결가는 각자 trig라 순서가 결과를 바꾸지 않음).
        매도 대금은 이 구간 이후의 매수에만 사용 가능.
        """
        seg = f"down_{a}{b}"
        for sym, info in day.items():
            p_a, p_b = info[a.lower()], info[b.lower()]
            hits = self._sell_hits(sym, info, lambda trig: le(p_b, trig) and le(trig, p_a))
            if not hits:
                continue
            amb = sym in self._bought_today or self._buy_reach(sym, info)
            for u, trig, why in sorted(hits, key=lambda x: -x[1]):
                self._sell(sym, date, u, trig, why + reason_suffix, seg, ambiguous=amb)

    def _seg_up(self, date, day, a: str, b: str, exclude_exited: bool):
        """
        상승 구간 a→b (예: O→H, L→H, L→C). 전 종목에 대해: 그 시점에 활성인 매수 주문 중 기준가 ∈ (P_a, P_b] 인 것을
        기준가에 체결. 기존 포지션 추가 → 신규 진입 순, 각각 강도순(D-9). 기준가 ≤ P_a 인 주문은 이미 앞 구간/시가에서
        처리됐거나(시가 케이스) 상향 통과가 없으므로 체결하지 않는다.
        """
        seg = f"up_{a}{b}"
        adds, entries = [], []
        for sym, info in day.items():
            if not (info["n_used"] > 0) or (exclude_exited and sym in self._exited_today):
                continue
            p_a, p_b = info[a.lower()], info[b.lower()]
            pos = self.positions.get(sym)
            if pos:
                if len(pos.units) < self.cfg.max_units_per_symbol and ge(p_b, pos.next_add_level) and pos.next_add_level > p_a + EPS:
                    adds.append((sym, info["t"], info["n_used"]))
            elif not np.isnan(info["brk"]) and ge(p_b, info["brk"]) and info["brk"] > p_a + EPS:
                entries.append((sym, info["t"], info["n_used"]))
        for sym, t, n_used in self._rank(adds):
            self._chain_adds(sym, date, day[sym], price_cap=day[sym][b.lower()], at_open=False, segment=seg)
        for sym, t, n_used in self._rank(entries):
            info = day[sym]
            u = self._buy(sym, date, info["brk"], info["brk"], n_used, "ENTRY", seg)
            if u is not None:
                st = self._strength(sym, t, n_used)
                self.events[-1]["strength_rank"] = round(st, 3) if np.isfinite(st) else None
                self._chain_adds(sym, date, info, price_cap=info[b.lower()], at_open=False, segment=seg)

    # 정책별 경로 정의: (방향, 시작, 끝, 옵션)
    PATHS = {
        # 지정 정책: O→L 매도 → L→H 매수(오늘 매도 종목 제외) → H→L 재하락 가정으로 매수 후 손절(_SAMEDAY)
        "designated": [("down", "O", "L", {}), ("up", "L", "H", {"exclude_exited": True}), ("down", "H", "L", {"reason_suffix": "_SAMEDAY"})],
        "OHLC": [("up", "O", "H", {"exclude_exited": False}), ("down", "H", "L", {}), ("up", "L", "C", {"exclude_exited": False})],
        "OLHC": [("down", "O", "L", {}), ("up", "L", "H", {"exclude_exited": False}), ("down", "H", "C", {})],
    }

    def _mark_buy_ambiguity(self, day):
        """오늘 매수한 종목 중 매도 트리거에도 저가가 닿은 종목의 매수 이벤트에 ambiguous=True."""
        for sym, evs in self._bought_today.items():
            info = day.get(sym)
            if info is None:
                continue
            pos = self.positions.get(sym)
            reach = sym in self._exited_today
            if pos:
                reach = reach or any(le(info["l"], self._trig(u, info["exit_lvl"])[0]) for u in pos.units)
            if reach:
                for ev in evs:
                    ev["ambiguous"] = True

    # ----------------------------------------------------------------- 하루 처리
    def _process_day(self, date, active: Dict[str, int]):
        c = self.cfg
        # 연초: A = 전년 마지막 거래일 종가 NAV, 축소 단계 해제 [D-3, D-4]
        if self.equity_curve and pd.Timestamp(self.equity_curve[-1][0]).year != pd.Timestamp(date).year:
            self.year_base = self.equity
            self.dd_step = 0
        self._exited_today = set()
        self._bought_today = {}
        day = {sym: self._day_info(sym, t) for sym, t in active.items()}

        # [R3] 배당락일 권리 확정 — 그날 매매가 일어나기 전(전일 종가 시점) 보유 수량 기준. 당일 매수자는 제외, 당일 매도자는 유지.
        self._dividend_entitlements(date, active)

        self._segment_open(date, day)                                   # A (전 종목 동시)
        if c.intraday_order not in self.PATHS:
            raise ValueError(c.intraday_order)
        for direction, a, b, opt in self.PATHS[c.intraday_order]:       # B/C: 전 종목을 같은 구간으로 맞춰 전역 처리
            if direction == "down":
                self._seg_down(date, day, a, b, **opt)
            else:
                self._seg_up(date, day, a, b, **opt)
        self._mark_buy_ambiguity(day)

        # D. 배당 [D-13], 지급일 도달 대가 현금화, 데이터 종료/상폐 처리 [D-14], 평가
        for r in [r for r in self.receivables if r["pay_date"] <= pd.Timestamp(date)]:
            self.cash += r["amount"]
            self.receivables.remove(r)
            self._log(date=date, symbol=r["symbol"], event="RECEIVABLE_PAID", amount=round(r["amount"], 2), segment="D")
        for sym, t in active.items():
            sd = self.data[sym]
            pos = self.positions.get(sym)
            # 종목 데이터가 백테스트 종료일보다 먼저 끝나는 경우만 '종료 처리'. 백테스트 마지막 날의 보유는 그냥 보유(평가)다.
            if pos and t == len(sd.df) - 1 and pd.Timestamp(date) < self.run_end:
                self._terminal(sym, date, pos)
        # [R4] 기업행사 효력일 도달: 마지막 가격 행 이후 대기 중이던 현금 인수를 효력일에만 인식
        self._recognize_pending_terminals(date)
        # 평가: NAV = 현금 + 보유 주식 × 종가 (모든 정책 공통, 종가 사용). 미지급 대가·미확인 포지션은 별도 열
        for sym, t in active.items():
            self.last_close[sym] = self.data[sym].df["close"].iat[t]
        mtm = self.mark_to_market()
        self.equity = mtm["nav"]
        nav_complete = mtm["nav_complete"]
        if not nav_complete and c.on_unresolved == "halt":
            # [판호 1.3 ■2] 평가 불가 자산이 생기면 NAV 불완전. 불완전 NAV로 수량 계산·낙폭 판단을 이어가지 않는다.
            self._set_halt("NAV_INCOMPLETE",
                           f"미확인 자산 {len(self.unresolved)}건(진단값 {mtm['unresolved_diag']:,.2f}) — 확인된 평가액 소계 {mtm['nav_confirmed']:,.2f}")
        if self.halt is None:
            self._update_drawdown_step()
        if self.halt is not None and self.halt.get("date") is None:
            self.halt["date"] = pd.Timestamp(date)
            self.checkpoint = self._make_checkpoint(date, mtm)
            for e in self.events:
                if e.get("event") == "HALT" and e.get("date") is None:
                    e["date"] = date
        self.equity_curve.append((date, round(self.equity, 2), round(self.cash, 2), round(mtm["market_value"], 2),
                                  self._total_units(), self.dd_step, round(self._notional(), 2),
                                  round(mtm["receivables"], 2), round(mtm["unresolved_diag"], 2), nav_complete))

    def mark_to_market(self) -> dict:
        """
        평가 구성 요소. 모든 정책에서 종가 사용.
        nav_confirmed = 현금 + 상장 보유주식 × 종가 + 확정 미지급 대가   (확인된 평가액 소계)
        unresolved_diag = 미확인 자산의 진단 표시값(마지막 종가) — 확정 평가 아님
        nav = on_unresolved="halt"(기본): nav_confirmed, 단 미확인이 있으면 nav_complete=False (완전한 NAV 아님, 평가 중단)
              on_unresolved="continue_last_close_diag": nav_confirmed + unresolved_diag (진단 가정: 마지막 종가로 계속 평가), preliminary
        """
        mv = sum(p.shares * self.last_close[s] for s, p in self.positions.items())
        recv = sum(r["amount"] for r in self.receivables)
        unres = sum(u["shares"] * u["last_close"] for u in self.unresolved)
        confirmed = self.cash + mv + recv
        if self.cfg.on_unresolved == "continue_last_close_diag":
            nav, complete = confirmed + unres, True
        else:
            nav, complete = confirmed, not self.unresolved
        return dict(cash=self.cash, market_value=mv, receivables=recv, unresolved_diag=unres,
                    nav_confirmed=confirmed, nav=nav, nav_complete=complete)

    def _make_checkpoint(self, date, mtm) -> dict:
        return dict(date=pd.Timestamp(date), reason=self.halt["reason"], detail=self.halt["detail"],
                    cash=round(self.cash, 2), nav_confirmed=round(mtm["nav_confirmed"], 2),
                    unresolved_diag=round(mtm["unresolved_diag"], 2), year_base=self.year_base, dd_step=self.dd_step,
                    positions={s: [(u.unit_no, u.shares, u.fill, u.stop) for u in p.units] for s, p in self.positions.items()},
                    receivables=list(self.receivables), unresolved=list(self.unresolved))

    def _dividend_entitlements(self, date, active):
        """
        [D-13][R3] 일반 현금배당: 배당락일 당일 매매 전 보유 수량(= 전일 종가 시점 보유)이 권리 수량.
        dividends 값이 float면 D-13 단순화(배당락일 즉시 현금, pay_date_assumed=True),
        {"amount":, "pay_date":} 이면 pay_date까지 미수금(receivable)으로 관리 — 그 전엔 현금으로 못 씀.
        특별배당·주식배당 등은 지원 범위 밖(구조 분리).
        """
        d = pd.Timestamp(date)
        for sym in list(self.positions):
            sd = self.data[sym]
            if d not in sd.dividends:
                continue
            pos = self.positions[sym]
            spec = sd.dividends[d]
            if isinstance(spec, dict):
                per, pay = float(spec["amount"]), pd.Timestamp(spec["pay_date"])
                assumed = False
            else:
                per, pay, assumed = float(spec), d, True
            shares = pos.shares
            amt = shares * per
            if amt <= 0:
                continue
            if pay <= d:
                self.cash += amt
                self._log(date=date, symbol=sym, event="DIVIDEND", amount=round(amt, 2), shares=shares, per_share=per,
                          segment="pre", pay_date=pay.date(), pay_date_assumed=assumed)
            else:
                self.receivables.append(dict(pay_date=pay, amount=amt, symbol=sym, kind="dividend"))
                self._log(date=date, symbol=sym, event="DIVIDEND_RECEIVABLE", amount=round(amt, 2), shares=shares,
                          per_share=per, segment="pre", pay_date=pay.date())

    def _recognize_pending_terminals(self, date):
        """[R4] effective_date 에 도달한 대기 기업행사(현금 인수)를 인식: 유닛 정산 → pay_date 미수금."""
        c = self.cfg
        d = pd.Timestamp(date)
        for sym, pos in list(self.positions.items()):
            ta = self.data[sym].terminal_action
            if not getattr(pos, "pending_terminal", False) or not ta:
                continue
            eff = pd.Timestamp(ta["effective_date"])
            if d < eff:
                continue
            pay, px, total = pd.Timestamp(ta["pay_date"]), float(ta["per_share"]), 0.0
            for u in list(pos.units):
                amt = u.shares * px
                total += amt
                pnl = (px - u.fill) * u.shares - u.shares * u.fill * c.commission_buy
                self._log(date=date, symbol=sym, event="CASH_DEAL", unit=u.unit_no, price=px, shares=u.shares,
                          entry_price=round(u.fill, 6), pnl=round(pnl, 2), effective_date=eff.date(), pay_date=pay.date(),
                          segment="D")
                pos.units.remove(u)
            self.receivables.append(dict(pay_date=pay, amount=total, symbol=sym, kind="cash_deal"))
            del self.positions[sym]

    def _terminal(self, sym, date, pos):
        """
        [D-14] 종목 가격 데이터의 마지막 봉에서 포지션이 남아 있을 때.
        - 현금 인수 자료 있음: 유닛을 per_share로 정산하되 대가는 pay_date까지 미지급 채권(receivable). 그 전엔 다른 매수에 못 씀.
        - 주식교환/혼합 자료: 현재 엔진은 모델링 불가 → 미확인(unresolved)으로 기록.
        - 자료 없음(기본): 미확인으로 기록. 마지막 종가는 진단용 표시값일 뿐 현금화하지 않는다.
          delisting_date가 없으면 '데이터 종료'일 뿐이며 상폐일로 간주하지 않는다(사유: data_end_only).
        - 옵션 delist_fill="last_close_diagnostic": 마지막 종가×(1−haircut)로 현금화 — 진단용 시나리오 전용, 이벤트에 diagnostic=True.
        """
        c = self.cfg
        sd = self.data[sym]
        ta = sd.terminal_action
        last_close = sd.df["close"].iat[-1]
        if ta and ta.get("type") == "cash":
            # [R4] 가격 관측 마지막 날 ≠ 권리 발생일. effective_date·pay_date 가 모두 있어야 인식 대상. 없으면 미확인.
            missing = [k for k in ("per_share", "effective_date", "pay_date") if ta.get(k) is None]
            if not missing:
                eff = pd.Timestamp(ta["effective_date"])
                if eff < pd.Timestamp(date):
                    missing = ["effective_date<last_price_date"]
            if missing:
                reason = "UNRESOLVED_TERMINAL_DATA_INCOMPLETE"
                for u in list(pos.units):
                    self.unresolved.append(dict(symbol=sym, unit=u.unit_no, shares=u.shares, entry_price=u.fill,
                                                last_close=last_close, last_date=date, reason=reason,
                                                delisting_date=sd.delisting_date, missing=missing))
                    self._log(date=date, symbol=sym, event=reason, unit=u.unit_no, shares=u.shares, segment="D",
                              note=f"현금 인수 자료 불완전 {missing} — 파일 종료일로 대체하지 않음")
                    pos.units.remove(u)
                del self.positions[sym]
                return
            # 효력일까지 대기: 가격 관측은 끝났지만 권리는 아직 발생 전. 평가는 마지막 종가(stale)로 계속하고 preliminary 표시
            pos.pending_terminal = True
            self._log(date=date, symbol=sym, event="PENDING_CORPORATE_ACTION", shares=pos.shares, segment="D",
                      effective_date=eff.date(), pay_date=pd.Timestamp(ta["pay_date"]).date(), per_share=float(ta["per_share"]),
                      note="마지막 가격 행. 효력일에 CASH_DEAL 인식, 지급일에 현금화")
            self._recognize_pending_terminals(date)      # 효력일 == 마지막 가격일이면 즉시
            return
        if c.delist_fill == "last_close_diagnostic" and ta is None:
            px = last_close * (1 - c.delist_haircut)
            for u in list(pos.units):
                self._sell(sym, date, u, px + c.slippage(), "DELIST_DIAGNOSTIC", "D")
            self.events[-1]["diagnostic"] = True
            return
        reason = ("UNRESOLVED_STOCK_DEAL" if ta else
                  "UNRESOLVED_DELISTED" if (sd.delisted or sd.delisting_date is not None) else
                  "UNRESOLVED_DATA_END")
        for u in list(pos.units):
            self.unresolved.append(dict(symbol=sym, unit=u.unit_no, shares=u.shares, entry_price=u.fill,
                                        last_close=last_close, last_date=date, reason=reason,
                                        delisting_date=sd.delisting_date))
            self._log(date=date, symbol=sym, event=reason, unit=u.unit_no, shares=u.shares, entry_price=round(u.fill, 6),
                      last_close=last_close, segment="D", note="최종 회수금액 미확인 — 현금화하지 않음, 진단값=마지막 종가")
            pos.units.remove(u)
        del self.positions[sym]

    def data_limits(self) -> dict:
        """결과 보고에 반드시 붙일 데이터 한계 표시."""
        return dict(
            unresolved_positions=len(self.unresolved),
            unresolved_diag_value=round(sum(u["shares"] * u["last_close"] for u in self.unresolved), 2),
            unresolved_reasons={r: sum(1 for u in self.unresolved if u["reason"] == r) for r in {u["reason"] for u in self.unresolved}},
            receivables_pending=round(sum(r["amount"] for r in self.receivables), 2),
            halted=self.halt is not None, halt=self.halt,
            pending_corporate_actions=sum(1 for p in self.positions.values() if getattr(p, "pending_terminal", False)),
            period=self.period_coverage(),
            full_period=(self.halt is None) and self.period_coverage()["requested_covered_by_data"],
            preliminary=bool(self.unresolved) or any(e.get("diagnostic") for e in self.events) or self.halt is not None
                        or any(getattr(p, "pending_terminal", False) for p in self.positions.values())
                        or not self.period_coverage()["requested_covered_by_data"],
        )

    # ----------------------------------------------------------------- 실행
    def run(self, start=None, end=None):
        data_dates = sorted({d for sd in self.data.values() for d in sd.df.index})
        self.data_start, self.data_end = (data_dates[0], data_dates[-1]) if data_dates else (None, None)
        self.requested_start = pd.Timestamp(start) if start is not None else None
        self.requested_end = pd.Timestamp(end) if end is not None else None
        all_dates = [d for d in data_dates
                     if (self.requested_start is None or d >= self.requested_start)
                     and (self.requested_end is None or d <= self.requested_end)]
        self.run_end = pd.Timestamp(all_dates[-1]) if all_dates else pd.Timestamp.max
        self.checkpoint = None
        self.last_processed_date = None
        self._ran = True
        for date in all_dates:
            active = {s: self.ind[s]["pos"][date] for s in self.data if date in self.ind[s]["pos"]}
            self._process_day(date, active)
            self.last_processed_date = pd.Timestamp(date)
            if self.halt is not None:           # 체크포인트를 남기고 평가 중단. 전체 기간 성과처럼 보고하지 않는다
                break
        return self

    def period_coverage(self) -> dict:
        """
        [R6] 요청 기간 / 데이터 기간 / 실제 처리 기간을 따로 보관. 요청 범위가 데이터로 채워지지 않으면 covered=False.
        영업일(bdate) 달력 근사로 누락 영업일 수를 보고한다(거래소 휴장일은 여기서 구분하지 않음 — 근사임을 표시).
        """
        rs, re_ = self.requested_start, self.requested_end
        ds, de = self.data_start, self.data_end
        lp = self.last_processed_date
        missing_start = (rs is not None and ds is not None and ds > rs)
        missing_end = (re_ is not None and (de is None or de < re_))
        gaps = 0
        if missing_start:
            gaps += len(pd.bdate_range(rs, ds - pd.Timedelta(days=1)))
        if missing_end:
            gaps += len(pd.bdate_range((de or rs) + pd.Timedelta(days=1), re_))
        return dict(requested_start=rs, requested_end=re_, data_start=ds, data_end=de, processed_end=lp,
                    requested_covered_by_data=not (missing_start or missing_end), missing_bdays_approx=gaps,
                    halted=self.halt is not None)

    # ----------------------------------------------------------------- 결과
    def events_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.events)

    def equity_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.equity_curve, columns=["date", "equity", "cash", "market_value", "units",
                                                        "dd_step", "notional", "receivables", "unresolved_diag", "nav_complete"]).set_index("date")

    def signal_snapshot(self, as_of=None) -> pd.DataFrame:
        """
        [R1] '다음 거래일에 지켜볼 가격' — 마지막 실제 처리일 **이하의 봉만** 사용한다.
        as_of 는 마지막 실제 처리일과 같은 값만 허용(다른 날짜는 거부: 가격은 절단되지만 positions·명목계좌는 최종 상태라 섞임).
        과거 시점 신호는 새 엔진을 run(end=그 날)로 실행해서 얻는다. 미래 행을 바꿔도 신호는 변하지 않는다. N은 as_of 종가까지 반영된 N_as_of
        (= 다음 거래일의 N_used, n_refresh=daily 기준).
        run() 전, 또는 HALT 상태(NAV 불완전·미규정 구간)에서는 주문 가능 신호를 만들지 않고 RuntimeError.
        """
        if not getattr(self, "_ran", False) or self.last_processed_date is None:
            raise RuntimeError("signal_snapshot: run()으로 처리된 날짜가 없다 — 신호 생성 불가")
        if self.halt is not None:
            raise RuntimeError(f"signal_snapshot: 엔진이 HALT 상태({self.halt['reason']}, {self.halt['date'].date()}) — "
                               f"불완전 계좌에서 주문 가능 신호를 생성하지 않는다")
        as_of = pd.Timestamp(as_of) if as_of is not None else self.last_processed_date
        if as_of != self.last_processed_date:
            # [판호 1.5] 가격만 절단하고 positions·명목계좌는 최종 상태를 쓰면 상태가 섞인다. 과거 계좌 상태 복원 기능은 만들지 않는다.
            raise RuntimeError(f"signal_snapshot: as_of({as_of.date()})는 마지막 실제 처리일({self.last_processed_date.date()})과 "
                               f"같아야 한다. 과거 시점 신호가 필요하면 새 엔진을 run(end={as_of.date()})로 실행하라.")
        rows = []
        c = self.cfg
        for sym, sd in self.data.items():
            if sd.delisted or sym in {u["symbol"] for u in self.unresolved}:
                continue
            df = sd.df
            t = int(df.index.searchsorted(as_of, side="right"))      # as_of 이하 봉 개수 = 다음 봉의 인덱스
            if t == 0 or t < max(c.entry_lookback, c.n_period + 1):
                continue
            h, l = df["high"].to_numpy(float)[:t], df["low"].to_numpy(float)[:t]
            hi = np.max(h[t - c.entry_lookback:t]); lo = np.min(l[t - c.exit_lookback:t])
            n_next = self.ind[sym]["n"][t - 1]                       # as_of 종가까지의 N
            pos = self.positions.get(sym)
            rows.append(dict(symbol=sym, as_of=as_of.date(), last_date=df.index[t - 1].date(), N=round(n_next, 4),
                             unit_shares=pos.unit_shares if pos else self._unit_shares(n_next),
                             entry_level=None if pos else round(hi + c.tick_size, 4),
                             add_level=round(pos.next_add_level, 4) if pos and len(pos.units) < c.max_units_per_symbol else None,
                             units=len(pos.units) if pos else 0,
                             stops=[round(u.stop, 4) for u in pos.units] if pos else None,
                             exit_level=round(lo - c.tick_size, 4) if pos else None))
        return pd.DataFrame(rows)
