"""내 텔레그램 chat_id 를 알아낸다.

사용법:
  1) @BotFather 에서 만든 봇을 텔레그램에서 찾아 /start 를 한 번 누른다
  2) set TELEGRAM_BOT_TOKEN=123456:AA...     (PowerShell: $env:TELEGRAM_BOT_TOKEN="...")
  3) python scripts/get_chat_id.py
"""

from __future__ import annotations

import os
import sys

import requests


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN 환경변수를 먼저 설정하세요.")
        print('  PowerShell:  $env:TELEGRAM_BOT_TOKEN = "123456:AA..."')
        return 1

    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    data = r.json()
    if not data.get("ok"):
        print("봇 토큰이 잘못된 것 같습니다:", data.get("description"))
        return 1

    found = {}
    for u in data.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            label = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
            found[chat["id"]] = f"{chat.get('type')} · {label}"

    if not found:
        print("메시지가 하나도 없습니다.")
        print("텔레그램에서 봇에게 /start 또는 아무 메시지나 보낸 뒤 다시 실행하세요.")
        return 1

    print("찾은 chat_id:")
    for cid, desc in found.items():
        print(f"  {cid}   ({desc})")
    print("\n개인 DM 으로 받으려면 type 이 'private' 인 숫자를 쓰세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
