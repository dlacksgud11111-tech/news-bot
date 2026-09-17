"""텔레그램 봇 API 래퍼 + 메시지 렌더링."""

from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timezone

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

    def send(self, text: str, preview: bool = False, channel: str | None = None) -> bool:
        ok = True
        for chunk in _split(text):
            res = self._call(
                "sendMessage",
                {
                    "chat_id": self.target(channel),
                    "text": chunk,
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": not preview},
                },
            )
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
    """요약 카드 → 텔레그램 HTML 메시지."""
    lines = [f"<b>[{_esc(card['title'])}]</b>", ""]

    lead = _esc(card.get("lead"))
    if lead:
        lines += [f"▪️ {lead}", ""]

    for n, d in enumerate(card.get("details") or [], 1):
        d = _esc(d)
        if d:
            lines.append(f"{n}. {d}")

    lines.append("")

    when = ""
    if item.get("published"):
        dt = datetime.fromtimestamp(item["published"], tz=timezone.utc)
        when = " · " + dt.astimezone().strftime("%m월 %d일")
    src = _esc(item.get("source") or "링크")
    lines.append(f'🔗 <a href="{_esc(item["link"])}">{src}{when}</a>')

    tags = [t.strip().lstrip("#").replace(" ", "") for t in (card.get("tags") or [])]
    tags = [f"#{_esc(t)}" for t in tags if t]
    if tags:
        lines.append(" ".join(tags))

    return "\n".join(lines)
