"""
실제 GitHub A→B 인계 1회 검증 드라이버 (판호 3단계).
- 상태는 '실제 비공개 원격 저장소(turtle-state)'에 저장/인계. 원격 URL은 env STATE_REMOTE(토큰 포함, 코드에 없음).
- 기존 가상 입력만 사용. 텔레그램은 목(mock) — 가짜 보유로 실제 알림 발송하지 않음.
- 확인: (A) A 상태가 원격에 저장 (B) B가 원격에서 이어받아 중복 입력/알림 반복 안 함
        (C) 단일 활성작업 잠금이 원격(lock.json 커밋)으로 강제 (D) 정상 인계 시 잠금 해제→획득 연쇄
        (E) 실제 작업종료→후속 시작 공백(runs.jsonl 시각차) 기록
사용: python3 live/remote_handoff_run.py --role A|B
"""
from __future__ import annotations
import argparse, json, os, sys, tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import state_store, device_free_job as dfj

TODAY = "2026-09-16"
def reg(h=14, m=0):
    return datetime(2026, 9, 16, h, m, tzinfo=timezone.utc)

# 가상 입력(기존 검증과 동일). A: U1 매수→ADD 발송. B: 중복 입력/알림 skip + U2 추가→새 ADD 발송.
FEEDS = {
    "A": [
        ("input", 0, "매수 AAPL 10 100 100 AAPL-C 2026-09-15"),   # add_line = 100 + 0.5*100 = 150
        ("tick", "AAPL", 149, reg(14, 0)),                        # 미달
        ("tick", "AAPL", 151, reg(14, 1)),                        # ADD 발생 → 발송
    ],
    "B": [
        ("input", 0, "매수 AAPL 10 100 100 AAPL-C 2026-09-15"),   # 이미 처리(uid0<offset) → skip
        ("tick", "AAPL", 151, reg(14, 30)),                       # 같은 ADD → 이미 발송 → dup_skip
        ("input", 1, "매수 AAPL 10 160 100 AAPL-C 2026-09-15"),   # U2 → add_line = 160+50 = 210
        ("tick", "AAPL", 215, reg(14, 31)),                       # 새 ADD(210) → 발송
    ],
}


class MockTG:
    """텔레그램 목 — 가짜 보유로 실제 발송하지 않음. send는 'sent' 반환(발송이력 dedup 검증용)."""
    def __init__(self): self.sent = []; self.replies = []
    def send(self, text): self.sent.append(text); return "sent"
    def reply(self, text): self.replies.append(text)


def append_run(store, rec: dict):
    path = os.path.join(store.root, "runs.jsonl")
    with open(path, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    store._commit(f"run {rec['role']} {rec['phase']}")


def read_runs(store):
    path = os.path.join(store.root, "runs.jsonl")
    out = []
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", required=True, choices=["A", "B"])
    args = ap.parse_args()
    role = args.role

    remote = os.environ.get("STATE_REMOTE")
    if not remote:
        print("STATE_REMOTE 환경변수 없음(원격 저장소 URL). 워크플로우 설정 확인.", flush=True)
        sys.exit(2)

    # 매번 새 클론 위치 → B가 원격에서 진짜로 다시 받아오는지 검증(로컬 잔상 아님)
    root = tempfile.mkdtemp(prefix=f"state_{role}_")
    store = state_store.StateStore(root, remote=remote)   # 시작 시 원격 pull(이어받기)

    start = datetime.now(timezone.utc).isoformat()
    append_run(store, {"role": role, "phase": "start", "utc": start})

    tg = MockTG()
    summ = dfj.run_active_job(store, f"job{role}", FEEDS[role], tg, TODAY, test=True)

    end = datetime.now(timezone.utc).isoformat()
    append_run(store, {"role": role, "phase": "end", "utc": end,
                       "sent": summ["sent"], "dup_skip": summ["dup_skip"],
                       "ambiguous": summ["ambiguous"], "inputs": summ["inputs"],
                       "offset": summ["final_offset"], "lock": summ["lock"]})

    print(f"\n===== 원격 인계 작업 {role} 결과 =====", flush=True)
    print(json.dumps(summ, ensure_ascii=False, indent=2), flush=True)
    print(f"원격 저장소 발송이력 sent_keys: {sorted(store.sent_keys())}", flush=True)
    print(f"원격 저장소 offset: {store.get_offset()}", flush=True)
    print(f"원격 저장소 positions 존재: {'AAPL-C' in store.load_positions_text()}", flush=True)

    if role == "A":
        ok = summ["lock"] and len(summ["sent"]) == 1 and store.get_offset() == 1 and "AAPL-C" in store.load_positions_text()
        print(f"\n[A 검증] 잠금 획득·입력 처리·ADD 발송·상태 원격 저장: {'PASS' if ok else 'FAIL'}", flush=True)
        print("→ 다음: role B 를 실행하면 이 상태를 원격에서 이어받는지 검증합니다.", flush=True)
        sys.exit(0 if ok else 1)

    # role B: 인계 검증
    runs = read_runs(store)
    a_end = next((r["utc"] for r in runs if r["role"] == "A" and r["phase"] == "end"), None)
    b_start = next((r["utc"] for r in runs if r["role"] == "B" and r["phase"] == "start"), None)
    gap = None
    if a_end and b_start:
        gap = (datetime.fromisoformat(b_start) - datetime.fromisoformat(a_end)).total_seconds()

    checks = {
        "B 잠금 획득(A 해제 후)": summ["lock"],
        "B가 uid0 재처리 안 함(offset 이어받음)": not any(i["uid"] == 0 for i in summ["inputs"]),
        "B가 이미 보낸 알림 재발송 안 함(dup_skip)": len(summ["dup_skip"]) >= 1,
        "B가 U2 새 입력 반영": any(i["uid"] == 1 for i in summ["inputs"]),
        "B가 새 조건 ADD만 1건 발송": len(summ["sent"]) == 1,
        "발송이력 원격 누적(A+B=2)": len(store.sent_keys()) == 2,
    }
    print("\n===== A→B 인계 검증 =====", flush=True)
    allok = True
    for k, v in checks.items():
        allok = allok and v
        print(f"  [{'PASS' if v else 'FAIL'}] {k}", flush=True)
    if gap is not None:
        print(f"\n  · 실제 작업종료(A)→후속시작(B) 공백: {gap:.1f} 초 "
              f"(수동 실행 간격 기준. 실제 cron은 예약 지연이 더해짐 → 무중단 아님)", flush=True)
    else:
        print("\n  · 공백 측정 불가(A 기록 없음 — A를 먼저 실행했는지 확인)", flush=True)
    print(f"\n인계 검증 종합: {'PASS' if allok else 'FAIL'}", flush=True)
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
