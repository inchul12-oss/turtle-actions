"""
연결 검증: turtle_live_stream 의 실제 수신 핸들러에 가짜 틱을 흘려 보유관리 알림이 나오는지 확인.
네트워크/실계정 없이 yfinance.WebSocket 만 가짜로 대체. 발송은 자격 없으면 'off'(큐 경로만 확인),
자격 있으면(인철 맥) 실제 [테스트]처럼 감. 실제 보유정보 아님(가상 체결).
"""
import os, sys, time, tempfile, types
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)

now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
HELD = "ZZTEST"

# 가짜 yfinance: subscribe 기록, listen 은 가상 틱 몇 개 흘리고 종료
class FakeWS:
    def __init__(self, verbose=False): self.msgs = None
    def subscribe(self, group): self.group = group
    def listen(self, handler):
        for px in (1040, 1060, 1060):      # 1040(미달) → 1060(ADD, add_line=1050)
            handler({"id": HELD, "price": px, "time": now_ms})
            time.sleep(0.05)
    def close(self): pass
fake = types.ModuleType("yfinance"); fake.WebSocket = FakeWS
sys.modules["yfinance"] = fake

import turtle_live_stream as tls

# 임시 levels.csv (보유종목 exit_level 제공) + positions.csv (가상 체결, 과거일=시각 불필요)
d = tempfile.mkdtemp()
lv = os.path.join(d, "levels.csv"); pos = os.path.join(d, "positions.csv")
with open(lv, "w") as f:
    f.write("symbol,status,as_of,entry_level,exit_level\n")
    f.write(f"{HELD},정상,2020-01-02,2000,800\n")     # entry 2000(관측 1060은 진입 미해당), L20 800
with open(pos, "w") as f:
    f.write("symbol,campaign_id,unit_no,fill_date,fill_time,fill_tz,fill_price,shares,trade_n,exit_date,exit_price,note\n")
    f.write(f"{HELD},{HELD}-2026-09,1,2026-01-02,,,1000,10,100,,,가상\n")   # 과거 체결, N=100 → add_line 1050

levels = tls.load_levels(lv)
summ = tls.run([HELD], levels, per_conn=100, minutes=0.05, gap=0.0,
               sec_class={}, notify=True, positions_path=pos)

print("\n=== 연결검증 결과 ===")
print("감시 보유종목:", summ["holding_watched"])
print("보유관리 알림 수:", summ["holding_alerts"])
for e in summ["holding_alerts_detail"]:
    print("  ->", e["symbol"], e["text_kind"], "기준", e["line"], "관측", e["recv_price"], "telegram=", e["telegram"], "첫관측부터=" , e["already"])
ok = summ["holding_alerts"] >= 1 and any(e["kind"] == "ADD" for e in summ["holding_alerts_detail"])
print("연결검증:", "PASS (수신→판정→발송큐 정상)" if ok else "FAIL")
sys.exit(0 if ok else 1)
