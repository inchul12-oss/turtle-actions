"""
상태 저장소 — 디바이스-프리 운영의 인계용. private repo(git) 1개에 상태를 '수시' 보존한다.
저장 항목: positions.csv(보유) · sent_history.csv(발송이력) · telegram_offset.txt(마지막 처리 update_id) · lock.json(활성작업 1개 제한).
- 공개 유출 방지: 이 저장소는 private. 공개 repo Actions 에서 PAT(Secret)로만 접근. 값은 로그·산출물로 내보내지 않는다.
- 수시 보존: 발송결과·체결변경·offset 을 그때그때 커밋(종료 때만 저장 아님).
- 인계: 다음 작업이 같은 저장소를 읽어 보유·offset·발송이력을 이어받는다.
- 발송/저장 사이 중단 대비: 발송 직전 'sending' 기록 → 성공 시 'sent'. 재시작 때 'sending'/'unknown' 은
  '발송 여부 불명'으로 두고 자동 재발송하지 않는다(맹목적 '중복·누락 없음' 주장 금지).

실사용(원격 private repo): 시작 시 pull, 커밋마다 push (여기선 로컬 git 로 인계 로직을 검증; 원격 연결은 설정 추가).
"""
from __future__ import annotations
import csv, json, os, subprocess, time
from datetime import datetime, timezone


class StateStore:
    def __init__(self, root: str, remote: str = None):
        self.root = os.path.abspath(root); os.makedirs(self.root, exist_ok=True)
        self.remote = remote
        self.pos = os.path.join(self.root, "positions.csv")
        self.hist = os.path.join(self.root, "sent_history.csv")
        self.offf = os.path.join(self.root, "telegram_offset.txt")
        self.lockf = os.path.join(self.root, "lock.json")
        if not os.path.isdir(os.path.join(self.root, ".git")):
            self._git("init", "-q")
            self._git("config", "user.email", "turtle-bot@local")
            self._git("config", "user.name", "turtle-bot")
            self._git("symbolic-ref", "HEAD", "refs/heads/main")   # 브랜치 main 고정(러너 기본이 master여도 일치)
        if self.remote:
            # 이미 origin 있으면 URL 갱신(토큰 회전 대비), 없으면 추가
            if self._git("remote", "get-url", "origin").returncode != 0:
                self._git("remote", "add", "origin", self.remote)
            else:
                self._git("remote", "set-url", "origin", self.remote)
            self.pull()

    def _git(self, *a):
        return subprocess.run(["git", "-C", self.root, *a], capture_output=True, text=True)

    def _commit(self, msg: str):
        self._git("add", "-A")
        r = self._git("commit", "-q", "-m", msg)
        if self.remote:
            p = self._git("push", "-q", "origin", "HEAD:main")
            if p.returncode != 0:
                # 원격이 앞서 있으면 rebase(내 커밋 보존)로 얹은 뒤 재시도 — reset은 유실되므로 금지
                self._git("fetch", "-q", "origin", "main")
                self._git("rebase", "origin/main")
                p2 = self._git("push", "-q", "origin", "HEAD:main")
                if p2.returncode != 0:
                    raise RuntimeError("state push 실패(원격 저장 확정 못함): " + (p2.stderr or p.stderr or "").strip()[:200])
        return r

    def pull(self):
        if self.remote:
            self._git("fetch", "-q", "origin", "main")
            self._git("reset", "--hard", "origin/main")   # 원격 main 없으면(빈 저장소) 무시됨

    # ---- 활성 작업 1개 제한 ----
    def acquire_lock(self, run_id: str, ttl: float = 900) -> bool:
        if os.path.exists(self.lockf):
            try:
                d = json.load(open(self.lockf))
            except Exception:
                d = {}
            if d.get("run_id") and d.get("run_id") != run_id and (time.time() - d.get("hb", 0)) < ttl:
                return False        # 다른 활성 작업 있음(신선한 lock)
        json.dump({"run_id": run_id, "hb": time.time(),
                   "since": datetime.now(timezone.utc).isoformat()}, open(self.lockf, "w"))
        self._commit(f"lock {run_id}")
        return True

    def heartbeat(self, run_id: str):
        json.dump({"run_id": run_id, "hb": time.time(),
                   "since": datetime.now(timezone.utc).isoformat()}, open(self.lockf, "w"))
        self._commit(f"hb {run_id}")

    def release_lock(self, run_id: str):
        if os.path.exists(self.lockf):
            try:
                if json.load(open(self.lockf)).get("run_id") != run_id:
                    return
            except Exception:
                pass
            os.remove(self.lockf); self._commit(f"unlock {run_id}")

    # ---- 보유 ----
    def load_positions_text(self) -> str:
        return open(self.pos).read() if os.path.exists(self.pos) else ""

    def save_positions_text(self, text: str):
        open(self.pos, "w").write(text)
        self._commit("positions update")

    # ---- 발송이력(수시 보존) ----
    def _latest_status(self):
        """키별 최신 상태(마지막 행 기준)."""
        latest = {}
        if os.path.exists(self.hist):
            for r in csv.DictReader(open(self.hist)):
                k = r.get("key")
                if k:
                    latest[k] = r.get("status")
        return latest

    def sent_keys(self) -> set:
        return {k for k, s in self._latest_status().items() if s == "sent"}

    def unknown_keys(self) -> set:
        """발송 여부 불명(발송 직전까지 갔으나 확정 못함) — 자동 재발송하지 않고 사람이 확인."""
        return {k for k, s in self._latest_status().items() if s in ("sending", "unknown")}

    def record(self, symbol: str, us_date: str, status: str, key: str, error: str = ""):
        newf = not os.path.exists(self.hist)
        with open(self.hist, "a", newline="") as f:
            w = csv.writer(f)
            if newf:
                w.writerow(["utc", "symbol", "us_date", "status", "error", "key"])
            w.writerow([datetime.now(timezone.utc).isoformat(), symbol, us_date, status, error, key])
        self._commit(f"hist {status} {key}")

    # ---- 텔레그램 처리 위치 ----
    def get_offset(self) -> int:
        try:
            return int(open(self.offf).read().strip())
        except Exception:
            return 0

    def set_offset(self, n: int):
        open(self.offf, "w").write(str(n))
        self._commit(f"offset {n}")
