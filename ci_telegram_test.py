import os, json, urllib.request
tok=os.environ.get("TELEGRAM_BOT_TOKEN"); chat=os.environ.get("TELEGRAM_CHAT_ID")
assert tok and chat, "TELEGRAM secrets 없음"
body=json.dumps({"chat_id":chat,"text":"[테스트] GitHub Actions 에서 발송 · 연결 확인"}).encode("utf-8")
r=urllib.request.urlopen(urllib.request.Request("https://api.telegram.org/bot%s/sendMessage"%tok, data=body, headers={"Content-Type":"application/json"}), timeout=15)
print("telegram ok:", json.loads(r.read().decode("utf-8")).get("ok"))
