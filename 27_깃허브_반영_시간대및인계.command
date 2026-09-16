#!/bin/bash
cd "$(dirname "$0")"
clear
echo "==============================================="
echo "  터틀 GitHub 반영 — 시간대 수정 + 원격 인계 워크플로우"
echo "  저장소: inchul12-oss/turtle-actions"
echo "==============================================="
echo
echo "[준비] GitHub 토큰(ghp_...)을 미리 만들어 두세요:"
echo "  https://github.com/settings/tokens/new  (repo 체크 -> Generate)"
echo "  (※ 이건 turtle-actions 업로드용. turtle-state 전용 토큰과 별개)"
echo

rm -f .git/*.lock .git/objects/*.lock .git/refs/heads/*.lock 2>/dev/null

# 반영 확인
MISS=0
grep -q "America/New_York" live/turtle_live_stream.py || { echo "❌ 시간대 수정 없음"; MISS=1; }
[ -f live/remote_handoff_run.py ] || { echo "❌ remote_handoff_run.py 없음"; MISS=1; }
[ -f live/state_store.py ] || { echo "❌ state_store.py 없음"; MISS=1; }
[ -f live/device_free_job.py ] || { echo "❌ device_free_job.py 없음"; MISS=1; }
[ -f .github/workflows/handoff_ab.yml ] || { echo "❌ handoff_ab.yml 없음"; MISS=1; }
[ "$MISS" = "1" ] && { echo; read -n1 -r -p "엔터..."; exit 1; }
echo "✅ 반영 파일 모두 확인됨."
echo

git reset --soft ad05c88 2>/dev/null
git add -A
echo "--- 이번에 올라갈 변경 ---"
git status --short
git -c user.name="turtle-bot" -c user.email="turtle-bot@local" \
    commit -q -m "시간대 America/New_York 수정 + 실제 A->B 원격 인계(turtle-state) 워크플로우/드라이버" 2>&1 | tail -2

printf "\nturtle-actions 업로드 토큰 붙여넣고 Enter (화면엔 안 보임): "
read -s TOK; echo
[ -z "$TOK" ] && { echo "토큰 비어있음. 종료."; read -n1 -r -p "엔터..."; exit 1; }
URL="https://inchul12-oss:${TOK}@github.com/inchul12-oss/turtle-actions.git"
echo "업로드 중..."
OUT=$(git push -f "$URL" main 2>&1); RC=$?
TOK=""; URL=""; unset TOK URL
echo "$OUT" | sed 's/ghp_[A-Za-z0-9_]*/***/g; s#https://[^@]*@#https://***@#g'
echo
if [ "$RC" = "0" ]; then
  echo "✅ 성공! GitHub 반영 완료."
  echo "   워크플로우 실행: https://github.com/inchul12-oss/turtle-actions/actions/workflows/handoff_ab.yml"
else
  echo "❌ 실패 (코드 $RC). 위 메시지를 형배에게 보여주세요."
fi
echo
read -n1 -r -p "엔터를 누르면 창이 닫힙니다..."
