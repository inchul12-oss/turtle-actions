"""
터틀 System 2 (55일 돌파, 주식 매수 전용) 설정값.

원칙: 원문(OriginalTurtle-Trading-Rules.pdf)에 없는 가정은 전부 여기 설정값으로 분리한다.
각 항목 주석의 [원문 pN] = 원문 페이지, [가정 D-n] = RULES.md의 구현 가정/미결 결정 번호.
STEP 1.1 (판호 검수 반영): 기본값 = 판호가 확정한 기준 실행(baseline) 값.
"""
from dataclasses import dataclass, field
from typing import Optional, Dict


@dataclass
class TurtleConfig:
    # ---------- 진입/청산 창 ----------
    entry_lookback: int = 55        # [원문 p20] System 2: 직전 55거래일 고가 (당일 제외)
    exit_lookback: int = 20         # [원문 p27] System 2: 직전 20거래일 저가 (당일 제외)
    tick_size: float = 0.01         # [가정 D-2] 주식 호가단위. 돌파 = 직전 고가 + 1틱 [원문 p19 "single tick"]

    # ---------- N (변동성) ----------
    n_period: int = 20              # [원문 p13] N = 20일 EMA(True Range), N_t = (19*N_{t-1} + TR_t)/20
    n_seed: str = "sma"             # [원문 p13] 초깃값은 직전 20일 TR 단순평균
    n_refresh: str = "daily"        # [가정 D-1] "daily" = t일 판단에 N_{t-1} / "weekly" = 매주 첫 거래일 직전 값 고정 (원문 p16 월요일 시트)
    # [가정 D-1 — 판호 1.2 재검토: 원형 확정 규칙으로 승인되지 않음. 재현용 보존] 캠페인(최초 진입~전량 청산) 동안
    # N과 목표 유닛 수량을 최초 진입 시점 값으로 고정. 원문은 추가 유닛 수량/추가 간격 N/손절 조정 N을 명시하지 않는다(RULES §14).
    # N 변화만으로 기존 손절가를 낮추지 않는다(손절은 유닛 추가 시에만 ½N씩 인상).
    campaign_fixed_n_and_size: bool = True

    # ---------- 수량 ----------
    risk_per_unit: float = 0.01     # [원문 p15] Unit = 1% 계좌 / (N × 1주당 달러). "1N 변동 = 계좌 1%" (2N 손절 위험은 비용·갭 전 약 2%)
    equity_basis: str = "year_base"  # [가정 D-3, 판호 확정] "year_base" = 연초 기준금액 A(전년 마지막 종가 NAV, 첫해 초기자본), 연중 이익으로 늘리지 않음
    #                                  "nav" = 전일 NAV / "fixed" = 초기자본 고정 (민감도용)
    notional_drawdown_rule: bool = True   # [원문 p18] A 대비 손실이 0.10A / 0.18A / 0.244A … 도달 시 명목 = 0.8A / 0.64A / 0.512A …
    #                                       종가로 판단, 다음 거래일 신규 주문부터 반영. NAV ≥ A 회복 시 해제. [가정 D-4]
    partial_unit: str = "skip"      # [가정 D-5, 판호 확정] 현금 부족 시 "skip". ("partial"은 기준 실행에서 비활성)
    min_shares: int = 1

    # ---------- 추가 매수(피라미딩) ----------
    pyramid_step_n: float = 0.5     # [원문 p20] 직전 실제 체결가 + ½N 마다 1유닛 추가
    max_units_per_symbol: int = 4   # [원문 p17, p20] 종목당 최대 4유닛 (발행사 단위로 합산 [가정 D-15])
    allow_multi_add_per_day: bool = True  # [원문 p21] 급등 시 하루에 4유닛까지 가능

    # ---------- 손절 ----------
    stop_n: float = 2.0             # [원문 p23] 손절 = 체결가 − 2N
    stop_policy: str = "raise_half_n"  # [원문 p23 "raised by ½N"] 유닛 추가마다 기존 유닛 손절가를 각각 ½N 인상 (판호 1번 지적 반영, 기본)
    #                                    "last_unit_minus_2n" = STEP 1 구버전(알려진 결함: 중간 갭에서 원문과 다름). 재현 전용.
    allow_known_defect_policy: bool = False   # True로 명시하지 않으면 결함 정책 선택 시 ValueError (정상 실행에서 격리)

    # ---------- 보유 한도 ----------
    max_units_total: int = 12       # [원문 p17] 한 방향 최대 12유닛 (주식은 롱만이므로 롱 합계)
    max_units_close_corr: int = 6   # [원문 p17] 밀접 상관군 6유닛 — 주식용 그룹 정의는 미결 [D-8], 매핑 없으면 미적용(결과에 표시)
    max_units_loose_corr: int = 10  # [원문 p17] 느슨한 상관군 10유닛 — 같음

    # ---------- 동시 신호 / 하루 처리 순서 ----------
    rank_by: str = "n_advance_3m"   # [원문 p30][가정 D-9] 같은 세그먼트의 매수는 '전일까지 자료'로 만든 강도순. "symbol" = 알파벳순(민감도)
    rank_lookback: int = 63         # [가정 D-9] 3개월 ≈ 63거래일
    # [가정 D-7] 일봉 내 순서 불명에 대한 지정 처리 정책 (RULES §9). 모든 정책은 세그먼트 A(시가 확정 체결) 뒤에
    # 아래 경로 구간을 전 종목에 대해 전역으로 순서대로 처리한다(engine.TurtleEngine.PATHS):
    #   "designated" = O→L 매도 → L→H 매수(오늘 매도 종목 제외) → H→L 재하락 가정 매도(_SAMEDAY)   ※ 물리 경로 아님. 진단·재현 전용, 성과 평가 제외
    #   "OHLC"       = O→H 매수 → H→L 매도 → L→C 매수            (경로 O→H→L→C, 민감도용)
    #   "OLHC"       = O→L 매도 → L→H 매수 → H→C 매도            (경로 O→L→H→C, 민감도용)
    intraday_order: str = "OHLC"    # [판호 판정 1.2] 기본 성과 평가 = OHLC 와 OLHC 를 둘 다 실행해 병기(evaluation_policies). designated 는 진단·재현용
    evaluation_policies: tuple = ("OHLC", "OLHC")

    # ---------- 비용 ----------
    commission_buy: float = 0.0010  # [가정 D-11] 토스 매수 0.10%
    commission_sell: float = 0.00102  # [가정 D-11] 토스 매도 0.10% + SEC fee 0.002%
    slippage_ticks: float = 1.0     # [가정 D-12] 체결가에 1틱 불리하게 (매수 +, 매도 −)

    # ---------- 기업행사 ----------
    dividend_handling: str = "cash_on_exdate"  # [가정 D-13] 배당은 배당락일에 현금 입금, 가격은 분할만 조정
    delist_fill: str = "unresolved"            # [D-14, 판호 판정] 기본: 자료 없는 상폐/데이터 종료는 '최종 회수금액 미확인'으로 기록, 현금화 안 함.
    #                                            "last_close_diagnostic" = 마지막 종가×(1−haircut) 현금화 — 진단용 시나리오 전용
    delist_haircut: float = 0.0                # [D-14] 진단용 시나리오에서만 의미
    # [판호 1.3 ■2] 미확인 자산이 생겼을 때: "halt"(기본) = NAV 불완전 → 체크포인트 저장 후 평가 중단(전체 기간 성과로 보고 금지)
    #                                     "continue_last_close_diag" = 마지막 종가로 계속 평가 — 별도 진단 가정, preliminary 표시
    on_unresolved: str = "halt"
    # 거래정지: 봉이 없는 날은 건너뛰고 창(55/20/N)은 존재하는 봉 기준 [가정 D-17]

    # ---------- 기타 ----------
    start_equity: float = 100_000.0
    warmup_bars: int = field(default=56)

    KNOWN_DEFECT_POLICIES = ("last_unit_minus_2n",)

    def __post_init__(self):
        if self.stop_policy in self.KNOWN_DEFECT_POLICIES and not self.allow_known_defect_policy:
            raise ValueError(f"stop_policy={self.stop_policy!r} 는 알려진 결함 정책(재현 전용)입니다. "
                             f"allow_known_defect_policy=True 를 명시해야 실행됩니다.")

    def slippage(self) -> float:
        return self.slippage_ticks * self.tick_size
