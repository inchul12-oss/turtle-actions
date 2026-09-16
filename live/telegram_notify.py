"""
텔레그램 발송 — 터틀 진입 후보 알림. 수신 스레드와 분리(큐+워커)해서 감시가 막히지 않게 한다.

토큰/chat_id 출처(대화·로그·ZIP 에 토큰 안 남김):
  1) 환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
  2) 없으면 로컬 파일(기본 ~/.turtle_telegram, 형식: KEY=VALUE 한 줄씩)
  둘 다 없으면 발송 '비활성'(감시·터미널·CSV 는 그대로 진행).

중복 방지: (종목, 미국 거래일) 키를 sent_history.csv 에 저장. status=='sent' 인 키는 재발송 안 함
  → 같은 날 재실행해도 반복 발송 없음.
재시도: 최대 3회 백오프. 전송 후 응답 불명(타임아웃 등)은 status='unknown' 으로 남기고,
  재실행 시 재발송될 수 있음(중복 가능성) → 한계로 보고.
로그: telegram_log.jsonl (상태만, 토큰 없음).
"""
from __future__ import annotations

import csv
import json
import os
import queue
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

API = "https://api.telegram.org/bot{token}/sendMessage"


def load_creds():
    """토큰·chat_id 를 환경변수 → 로컬 파일 순으로 읽는다. 반환값은 로그에 남기지 않는다."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        path = os.path.expanduser(os.environ.get("TURTLE_TELEGRAM_FILE", "~/.turtle_telegram"))
        if os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k == "TELEGRAM_BOT_TOKEN" and not tok:
                    tok = v
                elif k == "TELEGRAM_CHAT_ID" and not chat:
                    chat = v
    return tok, chat


class Notifier:
    def __init__(self, out_dir: str, us_date: str,
                 log_name="telegram_log.jsonl", hist_name="sent_history.csv",
                 max_retry=3, timeout=10):
        self.tok, self.chat = load_creds()
        self.enabled = bool(self.tok and self.chat)
        self.us_date = us_date
        self.max_retry = max_retry
        self.timeout = timeout
        self.log_path = os.path.join(out_dir, log_name)
        self.hist_path = os.path.join(out_dir, hist_name)
        self.q = queue.Queue()
        self.stats = Counter()
        self.lock = threading.Lock()
        self._queued = set()
        self.sent = self._load_sent()   # 이전 실행 포함, status=='sent' 인 (sym, us_date)
        self._stop = False
        self.worker = None
        if self.enabled:
            self.worker = threading.Thread(target=self._run, daemon=True)
            self.worker.start()

    def _load_sent(self):
        # 중복방지 키는 문자열. 진입 알림 기본키 = "SYM|us_date". 보유 알림은 enqueue(dedup_key=...) 로 상세키 전달.
        # 예전 형식(키 열 없음) 파일도 SYM|us_date 로 복원해 그대로 호환.
        s = set()
        if os.path.exists(self.hist_path):
            try:
                for r in csv.DictReader(open(self.hist_path)):
                    if r.get("status") == "sent":
                        s.add(r.get("key") or f'{r.get("symbol")}|{r.get("us_date")}')
            except Exception:
                pass
        return s

    def already_sent(self, sym: str) -> bool:
        return f"{sym}|{self.us_date}" in self.sent

    def enqueue(self, sym: str, text: str, dedup_key: str = None) -> str:
        """발송 큐에 넣는다(논블로킹). 이미 성공 발송/큐 대기 중인 키면 skip.
        dedup_key 미지정 시 (종목,거래일). 보유 알림은 종류·유닛·기준선서명이 담긴 키를 넘긴다."""
        if not self.enabled:
            return "disabled"
        key = dedup_key or f"{sym}|{self.us_date}"
        with self.lock:
            if key in self.sent or key in self._queued:
                self.stats["dup_skip"] += 1
                return "dup_skip"
            self._queued.add(key)
        self.q.put((sym, text, key))
        return "queued"

    def _post(self, text: str) -> bool:
        # JSON 본문(ensure_ascii=True → 한글이 \uXXXX 로 escape 되어 순수 ASCII 바이트) →
        # 환경 로케일과 무관하게 UnicodeEncodeError 안 남. 텔레그램 Bot API 는 JSON POST 허용.
        body = json.dumps({"chat_id": self.chat, "text": text,
                           "disable_web_page_preview": True}).encode("utf-8")
        req = urllib.request.Request(API.format(token=self.tok), data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("ok", False)

    def _run(self):
        while not (self._stop and self.q.empty()):
            try:
                sym, text, key = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            status, err = "failed", None
            for attempt in range(1, self.max_retry + 1):
                try:
                    if self._post(text):
                        status, err = "sent", None
                        break
                    status = "failed"
                except (socket.timeout, TimeoutError) as e:
                    # 요청은 보냈으나 응답을 못 받음 → 전송 여부 불명(중복 가능)
                    status, err = "unknown", f"{type(e).__name__}: {e}"
                except Exception as e:
                    status, err = "failed", f"{type(e).__name__}: {e}"
                if attempt < self.max_retry:
                    time.sleep(1.5 * attempt)
            self._record(sym, status, err, key)
            if status == "sent":
                with self.lock:
                    self.sent.add(key)
            self.q.task_done()

    def _record(self, sym, status, err, key=None):
        key = key or f"{sym}|{self.us_date}"
        row = {"utc": datetime.now(timezone.utc).isoformat(), "symbol": sym,
               "us_date": self.us_date, "status": status, "error": err, "key": key}
        try:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass
        newfile = not os.path.exists(self.hist_path)
        with open(self.hist_path, "a", newline="") as f:
            w = csv.writer(f)
            if newfile:
                w.writerow(["utc", "symbol", "us_date", "status", "error", "key"])
            w.writerow([row["utc"], sym, self.us_date, status, err or "", key])
        self.stats[status] += 1

    def send_raw(self, text: str) -> str:
        """[테스트]용 즉시 발송(큐 안 거침). 상태 문자열 반환."""
        if not self.enabled:
            return "disabled"
        try:
            return "sent" if self._post(text) else "failed"
        except Exception as e:
            return f"error: {type(e).__name__}"

    def stop(self, timeout=15):
        self._stop = True
        alive = False
        if self.worker:
            self.worker.join(timeout=timeout)
            alive = self.worker.is_alive()
        return dict(self.stats), alive


def build_message(sym, entry, px, basis_date, msg_utc_dt, already) -> str:
    """진입 후보 알림 메시지. 시세시각은 한국시간(KST=UTC+9, 날짜 포함)."""
    from datetime import timedelta
    kst = (msg_utc_dt + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S KST")
    typ = "첫 관측부터 기준가 이상 (돌파 순간 포착 아님)" if already else "관측 중 기준가 도달"
    return (f"[터틀 진입후보] {sym}\n"
            f"진입 기준가: {entry}\n"
            f"관측 가격: {px}\n"
            f"기준가 계산일: {basis_date}\n"
            f"시세시각(KST): {kst}\n"
            f"{typ}")


def build_holding_message(alert, n, msg_utc_dt, test=False) -> str:
    """보유관리 알림 메시지(진입후보와 다른 머리말). 실제 보유 알림엔 [테스트] 안 붙임.
    alert: holding_alerts.Alert. n: 캠페인 N. test=True 면 [테스트] 접두(실사용과 구분)."""
    from datetime import timedelta
    kst = (msg_utc_dt + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S KST")
    head = "[테스트] " if test else ""
    units = (" / ".join(alert.units)) if alert.units else "-"
    tag = " · 첫 관측부터 조건 충족(돌파 순간 포착 아님)" if alert.already else ""
    side = "매수" if alert.kind == "ADD" else "매도"
    return (f"{head}[보유관리·{side}] {alert.symbol} — {alert.text_kind}\n"
            f"기준선: {alert.line}\n"
            f"관측가: {alert.price}\n"
            f"대상 유닛: {units}\n"
            f"캠페인 N: {n}\n"
            f"시세시각(KST): {kst}{tag}\n"
            f"※ 자동주문 아님 · 실제 체결 입력(positions.csv) 기준 알림")
