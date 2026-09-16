"""
로컬 단일 실행 잠금 (판호 승인). 실사용 감시가 동시에 두 개 돌아가는 것을 막는다.

방식: OS 파일 잠금 flock(LOCK_EX|LOCK_NB).
 · 잠금은 '열린 파일 디스크립터'에 걸리므로 프로세스가 끝나면(정상 종료·강제 종료·크래시 모두)
   커널이 자동으로 해제한다. 잠금 파일이 남아 다음 실행을 막는 사고가 없다.
 · 파일 존재 여부로 판정하지 않는다(그 방식은 비정상 종료 시 영구히 막힌다).
 · 원격 인계용 lock.json(하트비트·TTL 기반)과는 별개 구조. 복제하지 않는다.

주의: 획득한 fd 는 프로세스가 살아있는 동안 절대 닫지 않는다(닫으면 잠금이 풀린다).
"""
from __future__ import annotations
import fcntl, json, os
from datetime import datetime, timezone


def acquire(path: str, info: dict = None):
    """반환 (fd, None) = 획득 성공 / (None, 기존보유자정보dict) = 이미 실행 중.
    성공 시 fd 는 호출자가 계속 들고 있어야 한다(닫지 말 것)."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        prev = {}
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 4096).decode("utf-8", "replace").strip()
            if raw:
                prev = json.loads(raw)
        except Exception:
            prev = {}
        os.close(fd)
        return None, prev
    rec = {"pid": os.getpid(),
           "started_utc": datetime.now(timezone.utc).isoformat(),
           "started_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    rec.update(info or {})
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(rec, ensure_ascii=False).encode("utf-8"))
        os.fsync(fd)
    except Exception:
        pass            # 정보 기록 실패해도 잠금 자체는 유효 — 감시를 막지 않는다
    return fd, None


def describe(prev: dict) -> str:
    if not prev:
        return "기존 실행 정보를 읽지 못했습니다(잠금은 유효)."
    return (f"PID {prev.get('pid','?')} · 시작 {prev.get('started_local') or prev.get('started_utc','?')}"
            f" · 모드 {prev.get('mode','?')}" + (f" · {prev.get('note')}" if prev.get('note') else ""))
