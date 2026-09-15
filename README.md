# turtle-actions (연결 확인용 최소 실행)

나스닥 터틀 System2 진입 후보 파이프라인이 GitHub Actions(클라우드)에서 도는지 확인하는 최소 시험.
맥에서 이미 끝낸 20→300→1,000→3,020 단계 시험을 반복하지 않는다. 이건 '접속·실행 되는가' 확인용.

## 준비
Settings → Secrets and variables → Actions 에 2개 추가:
- `TELEGRAM_BOT_TOKEN` : 봇 토큰
- `TELEGRAM_CHAT_ID`   : 알림 받을 chat_id

## 실행
Actions 탭 → turtle-test → Run workflow (수동 1회).
소수 종목(AAPL,MSFT,NVDA,KLAC,PFG)로 일봉 확보 → 기준가 계산 → 2분 스트리밍 → 텔레그램 [테스트] 발송.

## 포함/제외
- 포함: 실행에 필요한 코드·의존성·설정 예시만.
- 제외: 실제 보유내역/계좌금액/시세 원본/개인 식별값(.gitignore). 토큰은 코드에 없음(Secrets로만).

주의: 이 시험 성공 = 클라우드 접속·실행 확인. 장 전체 무중단 운영 완료 아님.
