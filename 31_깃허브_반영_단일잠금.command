#!/bin/bash
cd "$(dirname "$0")"
clear
echo "==============================================="
echo "  터틀 GitHub 반영 — 로컬 단일 실행 잠금"
echo "==============================================="
echo
echo "[준비] GitHub 토큰(ghp_...): https://github.com/settings/tokens/new (repo 체크)"
echo
rm -f .git/*.lock .git/objects/*.lock .git/refs/heads/*.lock 2>/dev/null
MISS=0
[ -f live/single_lock.py ] || { echo "❌ single_lock.py 없음"; MISS=1; }
grep -q "single_lock" live/turtle_live_stream.py || { echo "❌ 스트림에 잠금 미반영"; MISS=1; }
[ "$MISS" = "1" ] && { echo; read -n1 -r -p "엔터..."; exit 1; }
echo "✅ 단일 잠금 반영 확인."
echo
git add -A
echo "--- 올라갈 변경 ---"; git status --short
git -c user.name="turtle-bot" -c user.email="turtle-bot@local" \
    commit -q -m "로컬 단일 실행 잠금(flock) 추가 — 감시 중복 가동 방지" 2>&1 | tail -2
printf "\n토큰 붙여넣고 Enter (화면엔 안 보임): "
read -s TOK; echo
[ -z "$TOK" ] && { echo "토큰 비어있음."; read -n1 -r -p "엔터..."; exit 1; }
URL="https://inchul12-oss:${TOK}@github.com/inchul12-oss/turtle-actions.git"
OUT=$(git push -f "$URL" main 2>&1); RC=$?
TOK=""; URL=""; unset TOK URL
echo "$OUT" | sed 's/ghp_[A-Za-z0-9_]*/***/g; s#https://[^@]*@#https://***@#g'
echo
[ "$RC" = "0" ] && echo "✅ 반영 완료." || echo "❌ 실패(코드 $RC). 형배에게 보여주세요."
echo
read -n1 -r -p "엔터를 누르면 창이 닫힙니다..."
