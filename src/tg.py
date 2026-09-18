"""텔레그램 봇 API 래퍼 + 메시지 렌더링."""

from __future__ import annotations

import html
import logging
import re
import time
import urllib.parse

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # 텔레그램 한도 4096, 여유 둠


class Telegram:
    """채널 여러 개로 나눠 보낸다.

    channels 는 {용도: chat_id}. 해당 용도의 채널이 설정돼 있지 않으면
    개인 DM(chat_id)으로 떨어진다 — 채널을 아직 안 만들었어도 메시지가
    사라지지 않게 하기 위한 장치다.
    """

    def __init__(self, token: str, chat_id: str, channels: dict | None = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.channels = {k: str(v) for k, v in (channels or {}).items() if v}

    def target(self, channel: str | None) -> str:
        return self.channels.get(channel or "", self.chat_id)

    def _call(self, method: str, payload: dict, retries: int = 3) -> dict | None:
        url = API.format(token=self.token, method=method)
        for attempt in range(retries):
            try:
                r = requests.post(url, json=payload, timeout=30)
            except requests.RequestException as e:
                log.warning("텔레그램 접속 실패 (%d/%d): %s", attempt + 1, retries, e)
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                log.info("텔레그램 rate limit — %ds 대기", wait)
                time.sleep(wait + 1)
                continue

            data = r.json()
            if not data.get("ok"):
                log.error("텔레그램 %s 실패: %s", method, data.get("description"))
                return None
            return data.get("result")
        return None

    def send(self, text: str, preview: bool = False, channel: str | None = None,
             plain: bool = False) -> bool:
        """plain=True 면 HTML 해석 없이 글자 그대로 보낸다.

        주간 리포트처럼 '메시지를 그대로 복사해 붙이는' 용도에 쓴다. HTML 모드로
        보내려면 제목 속 & 나 < 를 &amp; 로 바꿔 넣어야 하는데, 그게 복사한
        글에 그대로 남아 리포트에 들어간다.
        """
        ok = True
        for chunk in _split(text):
            payload = {
                "chat_id": self.target(channel),
                "text": chunk,
                "link_preview_options": {"is_disabled": not preview},
            }
            if not plain:
                payload["parse_mode"] = "HTML"
            res = self._call("sendMessage", payload)
            ok = ok and res is not None
        return ok

    def get_updates(self, offset: int, timeout: int = 0) -> list[dict]:
        res = self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message"],
            },
            retries=1,
        )
        return res or []


def _split(text: str) -> list[str]:
    if len(text) <= MAX_LEN:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > MAX_LEN:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------- 렌더링

def _esc(s: str) -> str:
    return html.escape(str(s or "").strip(), quote=False)


def render(card: dict, item: dict) -> str:
    """요약 카드 → 텔레그램 메시지.

    주제 이모지 + 대괄호 제목, 줄표 리드, 1) 2) 항목을 빈 줄로 띄우고,
    맨 아래에 매체명으로 건 링크 한 줄. 해시태그는 넣지 않는다.
    발표 주체와 날짜는 첫 항목 안에 들어간다(프롬프트가 요구한다).
    """
    emoji = (card.get("emoji") or "📌").strip()
    lines = [f"<b>{_esc(emoji)} [{_esc(card['title'])}]</b>", ""]

    lead = _esc(card.get("lead"))
    if lead:
        lines += [f"- {lead}", ""]

    for n, d in enumerate(card.get("details") or [], 1):
        d = _esc(d)
        if d:
            lines += [f"{n}) {d}", ""]

    # 주소도 매체명도 노출하지 않는다. 누르기만 하면 되는 한 단어.
    lines.append(f'🔗 <a href="{_esc(item["link"])}">링크</a>')
    return "\n".join(lines)
