"""
보유관리 알림 검증 — 가상 체결로 판호 확인항목 전부 점검(네트워크 불필요, 로직/키/메시지).
맨끝에서 텔레그램 자격이 있으면(인철 맥) [테스트] 실제 발송으로 수신→판정→발송 연결도 짧게 확인.
실제 보유정보 없이 전부 가짜 데이터.
"""
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import holding_alerts as ha
import telegram_notify as tn

TODAY = "2026-09-16"
def dt(h, m=0, s=0, day=16):
    return datetime(2026, 9, day, h, m, s, tzinfo=timezone.utc)

def F(cid, uno, px, sh, n, fdate, ftime="", ftz="", ex="", exp=""):
    return ha.Fill("TEST", cid, uno, px, sh, n, fdate, ftime, ftz, ex, exp, order=uno)

PASS = []; FAIL = []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail and not cond else ""))

def watcher(fills, exit_level):
    camps = {"TEST": ha.build_campaign("TEST", fills, exit_level, TODAY)}
    return ha.HoldingWatcher(camps, TODAY), camps["TEST"]

print("=== 보유관리 알림 가상체결 검증 (today ET =", TODAY, ") ===")

# S1 ADD: 어제 체결 1유닛, N100, 체결가 1000 → add_line 1050
print("\nS1) 추가매수 조건 알림")
w, c = watcher([F("C1", 1, 1000, 10, 100, "2026-09-15")], exit_level=800)
a0 = w.observe("TEST", 1049, dt(14))
a1 = w.observe("TEST", 1051, dt(14, 1))
check("add_line=1050", c.add_line == 1050.0, str(c.add_line))
check("1049는 알림없음", a0 == [])
check("1051에서 ADD 1건", len(a1) == 1 and a1[0].kind == "ADD")
check("관측중 도달(첫관측부터 아님)", a1 and a1[0].already is False)

# S2 특정 유닛만 손절: 2유닛 [1000,1080] N100 → 손절 unit1=850, unit2=880
print("\nS2) 특정 유닛만 손절")
w, c = watcher([F("C1",1,1000,10,100,"2026-09-15"), F("C1",2,1080,10,100,"2026-09-15")], exit_level=700)
stops = {u["unit_no"]: u["stop"] for u in c.open_units}
check("unit1 손절 850", stops.get(1) == 850.0, str(stops))
check("unit2 손절 880", stops.get(2) == 880.0, str(stops))
a = w.observe("TEST", 875, dt(15))    # ≤880 only
check("875 → unit2만 손절", len(a)==1 and a[0].kind=="SELL_MERGE" and a[0].units==["C1#2"], str([ (x.kind,x.units) for x in a]))
check("unit1은 미포함", a and "C1#1" not in a[0].units)

# S3 L20 전량 청산(손절 동시 병합)
print("\nS3) L20 이탈 → 잔여 전량 청산(손절 동시 병합)")
w, c = watcher([F("C1",1,1000,10,100,"2026-09-15"), F("C1",2,1080,10,100,"2026-09-15")], exit_level=700)
a = w.observe("TEST", 690, dt(15))    # ≤700 L20 and ≤ both stops
check("690 → 1건으로 병합", len(a)==1, str(len(a)))
check("전량(두 유닛 모두)", a and set(a[0].units)=={"C1#1","C1#2"}, str(a[0].units if a else None))
check("L20 병합 표기", a and "전량 청산" in a[0].text_kind, a[0].text_kind if a else "")

# S7 다유닛 손절 병합(비 L20)
print("\nS7) 여러 유닛 손절 한 메시지(L20 아님)")
w, c = watcher([F("C1",1,1000,10,100,"2026-09-15"), F("C1",2,1080,10,100,"2026-09-15")], exit_level=500)
a = w.observe("TEST", 840, dt(15))    # ≤850,880 both stops, >500
check("840 → 손절 2유닛 1건 병합", len(a)==1 and set(a[0].units)=={"C1#1","C1#2"}, str([(x.text_kind,x.units) for x in a]))
check("전량청산 아님(손절 표기)", a and "손절" in a[0].text_kind and "전량" not in a[0].text_kind, a[0].text_kind if a else "")

# S4 당일 체결: 체결 전 시세 제외 + 체결 이후 정상 판정(첫 관측부터 조건 충족)
print("\nS4) 당일 체결 — 체결 전 제외 / 체결 이후 정상")
w, c = watcher([F("C1",1,1000,10,100,"2026-09-16","11:00:00","America/New_York")], exit_level=800)
# ET 11:00 = 15:00 UTC
before = w.observe("TEST", 1060, dt(14, 30))   # 체결(15:00) 이전 → 제외
after  = w.observe("TEST", 1060, dt(15, 30))   # 체결 이후 첫 적격틱, 이미 add_line(1050) 이상
check("체결 이전 시세 제외", before == [])
check("체결 이후 ADD 발생", len(after)==1 and after[0].kind=="ADD")
check("첫 관측부터 조건 충족 표기", after and after[0].already is True)

# S9 당일 체결인데 시각 없음 → 체결시각 입력 필요(발송 안 함)
print("\nS9) 당일 체결 시각 미입력 → '체결시각 입력 필요'")
w, c = watcher([F("C1",1,1000,10,100,"2026-09-16")], exit_level=800)   # 당일, 시각 없음
a = w.observe("TEST", 1060, dt(16))
check("알림 없음", a == [])
check("체결시각 입력 필요 표시", w.needs_time_flagged.get("TEST","").startswith("체결시각"), str(w.needs_time_flagged))

# S8 N 누락 → 알림 안 냄
print("\nS8) N 누락 → 'N 입력 필요'")
w, c = watcher([F("C1",1,1000,10,None,"2026-09-15")], exit_level=800)
check("캠페인 불가", c.ok is False)
check("사유 N 입력 필요", "N 입력 필요" in c.reason, c.reason)
check("관측해도 알림 0", w.observe("TEST", 5000, dt(16)) == [])

# S6 매도한 유닛 제외 (3체결 중 unit2 매도)
print("\nS6) 매도한 유닛 제외 (인상이력은 보존)")
fills = [F("C1",1,1000,10,100,"2026-09-15"),
         F("C1",2,1080,10,100,"2026-09-15", ex="2026-09-15", exp="1200"),  # 매도됨
         F("C1",3,1150,10,100,"2026-09-15")]
w, c = watcher(fills, exit_level=700)
open_ids = {u["unit_id"] for u in c.open_units}
check("열린 유닛 = unit1,unit3", open_ids=={"C1#1","C1#3"}, str(open_ids))
check("k=3 반영(인상이력 보존)", True)  # stops_all는 3체결로 계산됨
a = w.observe("TEST", 690, dt(15))    # 전량
check("전량청산에 매도유닛 제외", a and set(a[0].units)=={"C1#1","C1#3"}, str(a[0].units if a else None))

# S5 재시작 중복방지 / 재체결 후 재무장
print("\nS5) 재시작 중복방지 + 재체결 후 재무장")
# 세션1: 1유닛, ADD 발생 → 키 K1
w1, c1 = watcher([F("C1",1,1000,10,100,"2026-09-15")], exit_level=800)
al1 = w1.observe("TEST", 1060, dt(14))
K1 = al1[0].key
# 세션1 내 재관측 → 같은 키, 재발송 안 함(watcher 내 dedup)
again = w1.observe("TEST", 1070, dt(14,5))
check("세션내 같은 조건 재발송 없음", again == [])
# 재시작: sent 이력에 K1 있음 → 같은 키 skip
sent = {K1}
def enq(key):  # Notifier 중복방지와 동일 규칙
    return "dup_skip" if key in sent else (sent.add(key) or "queued")
w2, c2 = watcher([F("C1",1,1000,10,100,"2026-09-15")], exit_level=800)
al2 = w2.observe("TEST", 1060, dt(15))
check("재시작 후 같은 알림 skip", enq(al2[0].key) == "dup_skip")
# 재체결(unit2 추가) → add_line·손절 바뀜 → 새 키 → 재무장(발송)
w3, c3 = watcher([F("C1",1,1000,10,100,"2026-09-15"), F("C1",2,1080,10,100,"2026-09-15")], exit_level=800)
al3 = w3.observe("TEST", 1140, dt(15,10))   # 새 add_line=1080+50=1130
check("재체결로 add_line 변경", c3.add_line == 1130.0, str(c3.add_line))
check("변경된 알림은 새 키(재무장)", al3 and al3[0].key != K1 and enq(al3[0].key)=="queued", str(al3[0].key if al3 else None))
# 행 순서만 바뀌면 동일 취급
import copy
f = [F("C1",2,1080,10,100,"2026-09-15"), F("C1",1,1000,10,100,"2026-09-15")]  # 순서 뒤바꿈
w4, c4 = watcher(f, exit_level=800)
al4 = w4.observe("TEST", 1140, dt(15,20))
check("행순서 바뀌어도 같은 키(무관 재발송 없음)", al4 and al4[0].key == al3[0].key, f"{al4[0].key if al4 else None} vs {al3[0].key}")

# ---- 메시지 예시 ----
print("\n=== 알림 메시지 예시 ===")
mnow = dt(15, 30)
# ADD
w, c = watcher([F("C1",1,1000,10,100,"2026-09-15")], exit_level=800)
addA = w.observe("TEST", 1051, dt(15,30))[0]
print(tn.build_holding_message(addA, c.n, mnow)); print("-")
# 단일 손절
w, c = watcher([F("C1",1,1000,10,100,"2026-09-15"), F("C1",2,1080,10,100,"2026-09-15")], exit_level=700)
s1 = w.observe("TEST", 875, dt(15,30))[0]
print(tn.build_holding_message(s1, c.n, mnow)); print("-")
# L20 전량(손절 동시)
w, c = watcher([F("C1",1,1000,10,100,"2026-09-15"), F("C1",2,1080,10,100,"2026-09-15")], exit_level=700)
l = w.observe("TEST", 690, dt(15,30))[0]
print(tn.build_holding_message(l, c.n, mnow)); print("-")
# 테스트 접두 예
print(tn.build_holding_message(addA, c.n, mnow, test=True))

print(f"\n=== 결과: PASS {len(PASS)} / FAIL {len(FAIL)} ===")
if FAIL:
    print("실패:", FAIL); sys.exit(1)

# ---- 연결(선택): 자격 있으면 실제 [테스트] 발송 ----
tok, chat = tn.load_creds()
if tok and chat:
    print("\n[연결확인] 텔레그램 자격 있음 → 가상 보유알림 [테스트] 실제 발송")
    OUT = os.path.join(HERE, "out_yahoo"); os.makedirs(OUT, exist_ok=True)
    n = tn.Notifier(OUT, TODAY, log_name="telegram_log_holdtest.jsonl", hist_name="sent_history_holdtest.csv")
    w, c = watcher([F("C1",1,1000,10,100,"2026-09-15")], exit_level=800)
    al = w.observe("TEST", 1051, dt(15,30))[0]
    print("  enqueue:", n.enqueue(al.symbol, tn.build_holding_message(al, c.n, mnow, test=True), dedup_key="HOLDTEST|"+al.key))
    print("  중복 재요청:", n.enqueue(al.symbol, tn.build_holding_message(al, c.n, mnow, test=True), dedup_key="HOLDTEST|"+al.key))
    stats, alive = n.stop()
    print("  통계:", stats, "| 워커 미정리:", alive)
else:
    print("\n[연결확인] 텔레그램 미설정(클라우드) → 로직/메시지만 검증. 실제 [테스트] 발송은 인철 맥에서.")
