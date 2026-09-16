"""
보유관리 알림 — 실제 체결(positions.csv) 기준 추가매수·손절·L20 청산을 라이브 관측가로 판정.
통합지시 3번(판호 승인 반영). 기존 계산(turtle_levels 규칙)·발송(telegram_notify.Notifier)을 재사용한다.

핵심 규칙(판호 확정):
1) 라이브 관측가로만 판정. 당일 봉 고저가 판정 경로는 만들지 않는다.
   - 당일 체결은 fill_time+fill_tz 필수. 누락 시 그 알림은 '체결시각 입력 필요'로만 표시(발송 안 함).
   - 각 알림은 그 기준선을 정한 체결시각(effective_from) 이후 시세만 사용.
   - 추가 체결로 손절선이 바뀌면(마지막 체결 시각) 그 이후 시세부터 현재 선 적용.
2) N = positions.csv 의 trade_n(캠페인 N). 없거나 assumed 면 알림 안 냄 → 'N 입력 필요'.
   (turtle_levels 의 assumed 값이 알림으로 넘어가지 않게 한다.)
3) 매도한 유닛(exit 기록)은 알림 대상에서 제외. 단 손절 인상(½N) 이력 보존 위해 fills 개수에는 포함.
4) 한 관측에서 여러 유닛 손절 → 한 메시지로 묶음. L20 도 충족하면 '잔여 전량 청산'으로 합쳐 표시.
5) 중복방지/재설정: 키 = (종목, 미국거래일, 종류, 유닛/집합, 기준선서명). 서명은 유닛식별자·체결가·수량·N·
   체결시각·매도기록을 반영하되 CSV 행 순서와 무관. 실제 대상 유닛/기준선이 바뀐 알림만 재무장.

보유정보 비공개: positions.csv 는 로컬에만. GitHub 등 클라우드에서는 POSITIONS_CSV_B64(Secret) 를
임시파일로 만들어 읽고 종료 시 삭제. 로그·캐시·산출물에 원본을 남기지 않는다.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:                       # 3.8 대비(사용 환경은 3.11+)
    ZoneInfo = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from turtle.config import TurtleConfig                     # noqa: E402

CFG = TurtleConfig()
STOP_N = CFG.stop_n                     # 2.0
STEP_N = CFG.pyramid_step_n             # 0.5
MAX_UNITS = CFG.max_units_per_symbol    # 4
DEFAULT_TZ = "America/New_York"

POS_COLS = ["symbol", "campaign_id", "unit_no", "fill_date", "fill_time", "fill_tz",
            "fill_price", "shares", "trade_n", "exit_date", "exit_price", "note"]


def _sig(*parts) -> str:
    """순서 안정적 서명. float 는 소수 4자리로 고정해 표기 잡음 제거."""
    norm = []
    for p in parts:
        if isinstance(p, float):
            norm.append(f"{p:.4f}")
        elif isinstance(p, (list, tuple, set)):
            norm.append("|".join(sorted(str(x) for x in p)))
        else:
            norm.append("" if p is None else str(p))
    return hashlib.sha1("~".join(norm).encode("utf-8")).hexdigest()[:10]


# ----------------------------- 체결 로딩 -----------------------------
@dataclass
class Fill:
    symbol: str
    campaign_id: str
    unit_no: int
    fill_price: float
    shares: float
    trade_n: Optional[float]
    fill_date: str
    fill_time: str
    fill_tz: str
    exit_date: str
    exit_price: str
    order: int                          # 파일 내 원래 순서(안정 정렬 보조, 서명엔 안 씀)

    @property
    def unit_id(self) -> str:
        return f"{self.campaign_id}#{self.unit_no}"

    @property
    def is_open(self) -> bool:
        return not (str(self.exit_date).strip() or str(self.exit_price).strip())

    def fill_dt_utc(self, today_et: str) -> Tuple[Optional[datetime], bool]:
        """체결시각(UTC). (dt, needs_time). 과거일 체결은 시각 없어도 됨(오늘 어떤 틱이든 이후).
        당일 체결인데 시각 없으면 needs_time=True, dt=None."""
        d = str(self.fill_date).strip()[:10]
        if not d:
            return None, True
        t = str(self.fill_time).strip()
        tzname = str(self.fill_tz).strip() or DEFAULT_TZ
        if d < today_et:                # 과거 체결 → 오늘 모든 틱이 이후. 자정 ET 기준으로 둠.
            t = t or "00:00:00"
        elif not t:                     # 당일 체결 + 시각 없음 → 판정 불가
            return None, True
        try:
            hh = t if len(t.split(":")) == 3 else (t + ":00")
            naive = datetime.strptime(f"{d} {hh}", "%Y-%m-%d %H:%M:%S")
            tz = ZoneInfo(tzname) if ZoneInfo else timezone(timedelta(hours=-4))
            return naive.replace(tzinfo=tz).astimezone(timezone.utc), False
        except Exception:
            return None, True


def _read_rows(path: Optional[str], env_b64: str = "POSITIONS_CSV_B64") -> List[dict]:
    """로컬 CSV 또는 Secret(base64) 에서 체결 행을 읽는다. Secret 사용 시 임시파일로만 만들고 즉시 삭제."""
    text = None
    if path and os.path.exists(path):
        with open(path, newline="") as f:
            text = f.read()
    else:
        b64 = os.environ.get(env_b64)
        if b64:
            tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
            try:
                tmp.write(base64.b64decode(b64).decode("utf-8")); tmp.flush(); tmp.close()
                with open(tmp.name, newline="") as f:
                    text = f.read()
            finally:
                try:
                    os.remove(tmp.name)         # 종료 전 즉시 삭제(디스크에 원본 안 남김)
                except OSError:
                    pass
    if text is None:
        return []
    rows = []
    for i, r in enumerate(csv.DictReader(io.StringIO(text))):
        if (r.get("symbol") or "").strip().startswith("#"):
            continue
        r["_order"] = i
        rows.append(r)
    return rows


def load_fills(path: Optional[str] = None) -> Dict[str, List[Fill]]:
    """종목별 Fill 목록. campaign_id 필수(누락 시 해당 행 무시하고 표시는 상위에서)."""
    out: Dict[str, List[Fill]] = {}
    for r in _read_rows(path):
        sym = (r.get("symbol") or "").strip().upper()
        if not sym or not (r.get("fill_price") or "").strip() or not (r.get("shares") or "").strip():
            continue
        def num(k):
            v = (r.get(k) or "").strip()
            try:
                return float(v) if v not in ("", "nan", "None") else None
            except ValueError:
                return None
        f = Fill(symbol=sym, campaign_id=(r.get("campaign_id") or "").strip(),
                 unit_no=int(float(r.get("unit_no") or 0)), fill_price=num("fill_price") or 0.0,
                 shares=num("shares") or 0.0, trade_n=num("trade_n"),
                 fill_date=(r.get("fill_date") or "").strip(), fill_time=(r.get("fill_time") or "").strip(),
                 fill_tz=(r.get("fill_tz") or "").strip(), exit_date=(r.get("exit_date") or "").strip(),
                 exit_price=(r.get("exit_price") or "").strip(), order=int(r.get("_order", 0)))
        out.setdefault(sym, []).append(f)
    return out


# ----------------------------- 캠페인 → 기준선 -----------------------------
@dataclass
class Campaign:
    symbol: str
    ok: bool                            # 알림 가능 여부
    reason: str = ""                    # 불가 사유(예: N 입력 필요 / campaign_id 누락 / 열린 캠페인 없음)
    n: Optional[float] = None
    add_line: Optional[float] = None
    exit_level: Optional[float] = None
    open_units: List[dict] = field(default_factory=list)   # {unit_id, unit_no, stop, shares}
    last_fill_dt: Optional[datetime] = None
    last_fill_needs_time: bool = False
    first_open_dt: Optional[datetime] = None
    first_open_needs_time: bool = False
    n_open: int = 0
    sells_sig: str = ""                 # 매도 상태 서명(재무장 판단용)


def build_campaign(symbol: str, fills: List[Fill], exit_level: Optional[float], today_et: str) -> Campaign:
    """turtle_levels 규칙 그대로: 현재 캠페인의 유닛별 손절(raise_half_n)·다음 추가매수가.
    N 은 trade_n 필수(누락 시 알림 불가). exit_level 은 levels 계산값(L20−틱)."""
    fills = [f for f in fills if f.campaign_id]
    if len(fills) != len([f for f in fills]):
        pass
    if not fills:
        return Campaign(symbol, False, "campaign_id 누락(모든 체결 행에 campaign_id 필요)")
    open_ids = sorted({f.campaign_id for f in fills if f.is_open})
    if len(open_ids) > 1:
        return Campaign(symbol, False, f"열린 캠페인이 둘 이상 {open_ids} — 종목당 하나여야 함")
    if not open_ids:
        return Campaign(symbol, False, "열린 유닛 없음(캠페인 종료)")
    camp = [f for f in fills if f.campaign_id == open_ids[0]]
    camp.sort(key=lambda f: (f.fill_date, f.unit_no, f.order))     # 안정 정렬(행순서 무관)
    ns = [f.trade_n for f in camp if f.trade_n is not None]
    if not ns:
        return Campaign(symbol, False, "N 입력 필요(positions.csv trade_n 비어 있음 — 추정 안 함)")
    n = float(ns[0])
    k = len(camp)                       # 캠페인 전체 체결(닫힌 유닛 포함) — 손절 인상 이력
    fills_px = [f.fill_price for f in camp]
    stops_all = [px - STOP_N * n + STEP_N * n * (k - 1 - i) for i, px in enumerate(fills_px)]
    open_units = []
    open_dts = []
    for i, f in enumerate(camp):
        if f.is_open:
            open_units.append({"unit_id": f.unit_id, "unit_no": f.unit_no,
                               "stop": stops_all[i], "shares": f.shares})
            dt, nt = f.fill_dt_utc(today_et)
            open_dts.append((dt, nt))
    n_open = len(open_units)
    last = camp[-1]
    last_dt, last_nt = last.fill_dt_utc(today_et)
    # first open fill 시각
    first_dt = None; first_nt = False
    valid_dts = [d for d, nt in open_dts if d is not None]
    if valid_dts:
        first_dt = min(valid_dts)
    first_nt = any(nt for _, nt in open_dts) and not valid_dts   # 열린 유닛 시각이 전혀 없으면 필요
    add_line = (fills_px[-1] + STEP_N * n) if (1 <= n_open < MAX_UNITS) else None
    sells = [f.unit_id for f in fills if not f.is_open]
    sells_sig = _sig("SELLS", sells)
    return Campaign(symbol, True, "", n=n, add_line=add_line, exit_level=exit_level,
                    open_units=open_units, last_fill_dt=last_dt, last_fill_needs_time=last_nt,
                    first_open_dt=first_dt, first_open_needs_time=first_nt, n_open=n_open,
                    sells_sig=sells_sig)


# ----------------------------- 관측 → 알림 -----------------------------
@dataclass
class Alert:
    symbol: str
    kind: str                           # ADD / STOP / L20 / SELL_MERGE
    key: str                            # 중복방지 키
    text_kind: str                      # 사람이 읽을 종류
    line: float
    price: float
    already: bool                       # 첫 관측부터 조건 충족
    units: List[str] = field(default_factory=list)
    note: str = ""


class HoldingWatcher:
    """종목별 캠페인 기준선을 들고, 관측가마다 발생한 보유관리 알림을 만든다(발송은 Notifier가)."""
    def __init__(self, campaigns: Dict[str, Campaign], us_date: str):
        self.camps = campaigns
        self.us_date = us_date
        self._first_seen: Dict[str, bool] = {}     # spec_id -> (첫 적격틱에서 조건 충족?)
        self._seen_ids: set = set()
        self.needs_time_flagged: Dict[str, str] = {}   # symbol -> reason(표시용)

    def _specs(self, c: Campaign):
        """현재 기준선에서 감시할 항목들. eff_from(적용 시작 체결시각)·needs_time 포함."""
        specs = []
        if c.add_line is not None:
            sig = _sig("ADD", c.n, c.add_line, c.n_open, c.sells_sig)
            specs.append(dict(kind="ADD", cmp="ge", line=c.add_line, eff=c.last_fill_dt,
                              needs_time=c.last_fill_needs_time, units=[], sig=sig,
                              spec_id=f"{c.symbol}|ADD|{sig}"))
        for u in c.open_units:
            sig = _sig("STOP", u["unit_id"], u["stop"], c.n, c.sells_sig)
            specs.append(dict(kind="STOP", cmp="le", line=u["stop"], eff=c.last_fill_dt,
                              needs_time=c.last_fill_needs_time, units=[u["unit_id"]], sig=sig,
                              spec_id=f"{c.symbol}|STOP|{u['unit_id']}|{sig}"))
        if c.exit_level is not None and c.open_units:
            allu = [u["unit_id"] for u in c.open_units]
            sig = _sig("L20", c.exit_level, allu, c.sells_sig)
            specs.append(dict(kind="L20", cmp="le", line=c.exit_level, eff=c.first_open_dt,
                              needs_time=c.first_open_needs_time, units=allu, sig=sig,
                              spec_id=f"{c.symbol}|L20|{sig}"))
        return specs

    def observe(self, symbol: str, price: float, obs_dt: datetime) -> List[Alert]:
        c = self.camps.get(symbol)
        if not c or not c.ok:
            return []
        fired = []
        stop_hits = []      # (unit_id, line, already)
        l20_hit = None
        add_hit = None
        for sp in self._specs(c):
            if sp["needs_time"]:
                self.needs_time_flagged[symbol] = "체결시각 입력 필요(당일 체결 시각 미입력)"
                continue
            if sp["eff"] is None or obs_dt < sp["eff"]:
                continue                # 체결(적용) 시각 이전 시세는 사용 안 함
            cond = (price >= sp["line"]) if sp["cmp"] == "ge" else (price <= sp["line"])
            if sp["spec_id"] not in self._first_seen:      # 이 기준선의 첫 적격 관측
                self._first_seen[sp["spec_id"]] = cond     # 첫 관측부터 충족이면 True
            if not cond:
                continue
            already = self._first_seen[sp["spec_id"]]
            if sp["kind"] == "ADD":
                add_hit = (sp, already)
            elif sp["kind"] == "STOP":
                stop_hits.append((sp, already))
            elif sp["kind"] == "L20":
                l20_hit = (sp, already)
        # ADD (매수측) — 단독
        if add_hit:
            sp, already = add_hit
            key = f"{symbol}|{self.us_date}|ADD|{sp['sig']}"
            if key not in self._seen_ids:
                self._seen_ids.add(key)
                fired.append(Alert(symbol, "ADD", key, "추가매수 조건 도달", sp["line"], price, already))
        # 매도측 병합: 손절 여러 유닛 + L20 → 한 메시지
        if stop_hits or l20_hit:
            units = []
            for sp, _a in stop_hits:
                units += sp["units"]
            already_any = any(a for _s, a in stop_hits) or (l20_hit[1] if l20_hit else False)
            if l20_hit:
                sp = l20_hit[0]
                units = sp["units"]     # 잔여 전량
                tk = "전량 청산 (L20 이탈" + (" · 손절 동시" if stop_hits else "") + ")"
                sig = _sig("SELLMERGE", "L20", units, sp["line"], c.sells_sig,
                           *[s["sig"] for s, _ in stop_hits])
                line = sp["line"]
                kind = "SELL_MERGE"
            else:
                tk = ("손절" if len(units) == 1 else f"손절 {len(units)}유닛 동시")
                sig = _sig("SELLMERGE", "STOP", units, c.sells_sig,
                           *[s["sig"] for s, _ in stop_hits])
                line = min(s["line"] for s, _ in stop_hits)
                kind = "SELL_MERGE"
            key = f"{symbol}|{self.us_date}|SELL|{sig}"
            if key not in self._seen_ids:
                self._seen_ids.add(key)
                fired.append(Alert(symbol, kind, key, tk, line, price, already_any, units=units))
        return fired
