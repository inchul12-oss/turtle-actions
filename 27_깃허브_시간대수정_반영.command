#!/bin/bash
cd "$(dirname "$0")"
clear
echo "==============================================="
echo "  터틀 GitHub 반영 — 시간대 수정(America/New_York)"
echo "  저장소: inchul12-oss/turtle-actions"
echo "==============================================="
echo
echo "[준비] GitHub 토큰(ghp_...)을 미리 만들어 두세요:"
echo "  https://github.com/settings/tokens/new"
echo "  - Note: turtle  /  Expiration: 30 days  /  'repo' 체크  -> Generate token -> 복사"
echo

# 1) 남아있는 잠금 파일 정리(클라우드 쪽에서 못 지운 것)
rm -f .git/*.lock .git/objects/*.lock .git/refs/heads/*.lock 2>/dev/null

# 2) 수정본이 실제로 반영됐는지 확인
if ! grep -q "America/New_York" live/turtle_live_stream.py; then
  echo "❌ live/turtle_live_stream.py 에 시간대 수정이 없습니다. 형배에게 알려주세요."
  echo; read -n1 -r -p "엔터를 누르면 닫힙니다..."; exit 1
fi
echo "✅ 시간대 수정 확인됨 (America/New_York)."
echo

# 3) 직전 잘못된 임시 커밋이 있으면 되돌리고(작업내용은 유지), 깨끗하게 다시 커밋
BASE=ad05c88
git reset --soft "$BASE" 2>/dev/null
git add -A
git -c user.name="turtle-bot" -c user.email="turtle-bot@local" \
    commit -q -m "시간대 수정: UTC-4 고정 -> America/New_York(서머타임 자동)" 2>&1 | tail -2

# 4) 푸시
printf "토큰을 붙여넣고 Enter (화면엔 안 보임): "
read -s TOK; echo
if [ -z "$TOK" ]; then echo "토큰이 비어 있습니다. 종료."; read -n1 -r -p "엔터..."; exit 1; fi
URL="https://inchul12-oss:${TOK}@github.com/inchul12-oss/turtle-actions.git"
echo "업로드 중..."
OUT=$(git push -f "$URL" main 2>&1); RC=$?
TOK=""; URL=""; unset TOK URL
echo "$OUT" | sed 's/ghp_[A-Za-z0-9_]*/***/g; s#https://[^@]*@#https://***@#g'
echo
if [ "$RC" = "0" ]; then
  echo "✅ 성공! 시간대 수정이 GitHub에 반영됐습니다."
  echo "   확인: https://github.com/inchul12-oss/turtle-actions/commits/main"
else
  echo "❌ 실패 (코드 $RC). 위 메시지를 형배에게 보여주세요."
fi
echo
echo "(토큰은 파일에 저장되지 않았습니다. 끝났으면 토큰은 폐기해도 됩니다.)"
read -n1 -r -p "엔터를 누르면 이 창이 닫힙니다..."
