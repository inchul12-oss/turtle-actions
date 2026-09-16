#!/bin/bash
cd "$(dirname "$0")"
clear
echo "==============================================="
echo "  터틀 GitHub 최종 업로드 (17개 파일 한번에)"
echo "  저장소: inchul12-oss/turtle-actions"
echo "==============================================="
echo
echo "[준비] GitHub 토큰을 먼저 만들어 두세요:"
echo "  https://github.com/settings/tokens/new"
echo "  - Note: turtle  /  Expiration: 30 days"
echo "  - 'repo' 체크  ->  맨 아래 Generate token  ->  복사(ghp_...)"
echo
rm -f .git/index.lock 2>/dev/null
printf "토큰을 붙여넣고 Enter (화면엔 안 보임): "
read -s TOK
echo
if [ -z "$TOK" ]; then echo "토큰이 비어 있습니다. 종료."; exit 1; fi
URL="https://inchul12-oss:${TOK}@github.com/inchul12-oss/turtle-actions.git"
echo "업로드 중... (수 초 소요)"
OUT=$(git push -f "$URL" main 2>&1); RC=$?
TOK=""; URL=""; unset TOK URL
echo "$OUT" | sed 's/ghp_[A-Za-z0-9_]*/***/g; s#https://[^@]*@#https://***@#g'
echo
if [ "$RC" = "0" ]; then
  echo "✅ 성공! 17개 파일 GitHub 업로드 완료."
  echo "   확인: https://github.com/inchul12-oss/turtle-actions"
else
  echo "❌ 실패 (코드 $RC). 위 메시지를 형배에게 보여주세요."
fi
echo
echo "(보안: 방금 입력한 토큰은 파일에 저장되지 않았습니다."
echo " 업로드가 끝났으면 GitHub 토큰 페이지에서 폐기(delete)해도 됩니다.)"
echo
echo "이 창은 닫아도 됩니다."
