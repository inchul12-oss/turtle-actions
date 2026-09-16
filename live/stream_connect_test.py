"""
연결 검증: turtle_live_stream 의 실제 수신 핸들러에 가짜 틱을 흘려
(1) 정규장 틱 → 실사용에서 보유 판정/발송큐 정상
(2) 프리마켓 틱 → 실사용(test=False)에서는 판정 제외(정규장 기준)
(3) 프리마켓 틱 → 시험모드(test=True)에서는 허용(연결 시험용)
네트워크/실계정 없이 yfinance.WebSocket 만 가짜로 대체. 실제 보유정보 아님(가상 체결).
"""
import os, sys, time, tempfile, types
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
HELD = "ZZTEST"

def make_fake(msg_ms):
    class FakeWS:
        def __init__(self, verbose=False): pass
        def subscribe(self, group): self.group = group
        def listen(self, handler):
            for px in (1040, 1060, 1060):
                handler({"id": HELD, "price": px, "time": msg_ms}); time.sleep(0.03)
        def close(self): pass
    m = types.ModuleType("yfinance"); m.WebSocket = FakeWS; return m

def run_case(msg_ms, test_flag):
    sys.modules["yfinance"] = make_fake(msg_ms)
    import importlib
    tls = importlib.import_module("turtle_live_stream"); importlib.reload(tls)
    d = tempfile.mkdtemp()
    lv = os.path.join(d, "levels.csv"); pos = os.path.join(d, "positions.csv")
    open(lv, "w").write("symbol,status,as_of,entry_level,exit_level\n%s,정상,2020-01-02,2000,800\n" % HELD)
    open(pos, "w").write("symbol,campaign_id,unit_no,fill_date,fill_time,fill_tz,fill_price,shares,trade_n,exit_date,exit_price,note\n"
                         "%s,%s-C,1,2026-01-02,,,1000,10,100,,,가상\n" % (HELD, HELD))
    summ = tls.run([HELD], tls.load_levels(lv), per_conn=100, minutes=0.02, gap=0.0,
                   sec_class={}, notify=True, positions_path=pos, test=test_flag)
    return summ["holding_alerts"]

# 오늘(ET) 정규장 시각 = 오늘 14:00 UTC(10:00 ET), 프리마켓 = 현재(대략 07~08 ET)
today = datetime.now(timezone.utc)
reg_ms = int(datetime(today.year, today.month, today.day, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)  # 10:00 ET
pre_ms = int(datetime.now(timezone.utc).timestamp() * 1000)                                                 # 지금(프리마켓)

print("=== 정규장 게이트 연결 검증 ===")
a = run_case(reg_ms, False); print(f"(1) 정규장 틱 · 실사용: 보유알림 {a}건  ->", "PASS" if a >= 1 else "FAIL")
b = run_case(pre_ms, False); print(f"(2) 프리마켓 틱 · 실사용: 보유알림 {b}건  ->", "PASS" if b == 0 else "FAIL")
c = run_case(pre_ms, True);  print(f"(3) 프리마켓 틱 · 시험모드: 보유알림 {c}건  ->", "PASS" if c >= 1 else "FAIL")
ok = (a >= 1 and b == 0 and c >= 1)
print("연결/게이트 검증:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
