"""엔트리포인트.

한 번 실행될 때 하는 일:
  1) 내가 봇에게 보낸 메시지 처리 (링크 정리 / 명령어)   ← 모드 C
  2) 조용한 시간에 밀렸던 발행물 내보내기
  3) 뉴스 수집 → 선별 → 요약 → 발송                      ← 모드 A+B
  4) 금요일 08시가 지났으면 부동산 주간 News Flow          ← 모드 D
  5) 상태 저장
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import yaml

from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent))

import alerts  # noqa: E402
import archive  # noqa: E402
import clipping  # noqa: E402
import extract  # noqa: E402
import scoring  # noqa: E402
import sources  # noqa: E402
import tg  # noqa: E402
import vault  # noqa: E402
import weekly  # noqa: E402
from store import Store, now_kst, url_key  # noqa: E402
from summarize import Summarizer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("newsbot")
NEWLINE = chr(10)

def load_dotenv(path: Path) -> int:
    """로컬 테스트용 .env 로더.

    GitHub Actions 에서는 Secrets 가 이미 환경변수로 들어오므로,
    이미 설정된 값은 절대 덮어쓰지 않는다. 의존성 없이 쓰려고 직접 파싱한다.
    """
    if not path.exists():
        return 0
    loaded = 0
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and not os.environ.get(key):
            os.environ[key] = value
            loaded += 1
    return loaded


HELP = """<b>리서치 봇</b>

여기가 입력창입니다. 넣은 것은 📝 기사 요약 채널과 옵시디언 위키에 함께 저장됩니다.

• <b>링크</b> → 요약 카드 + 위키 아카이브
• <b>다른 채널 글 전달</b> → 본문으로 요약 + 위키 아카이브
• <b>#태그·메모</b>를 같이 쓰면 그대로 반영
  예) <code>#원전 웨스팅하우스 밸류 근거 https://…</code>
  섹터: #건설 #유틸리티 #원전 #반도체 #IT
  개인: #여행 #일정 #월급 (요약 없이 그 폴더에 저장)
• <b>블로그·긴 전달글</b> → 위키엔 긴 정리본 + 원문 사본
• <b>일기</b> → 매일 21:30 질문에 답장 (또는 #일기 붙여서 아무 때나)
• 링크 없는 메모 → 위키 메모함에 저장

/status — 오늘 발행 수, 큐, 일시정지 상태
/pause — 자동 발행 중지 (링크 정리는 계속 됨)
/resume — 자동 발행 재개
/help — 이 도움말"""


# ---------------------------------------------------------------- 공통

def in_quiet_hours(cfg: dict) -> bool:
    q = cfg.get("quiet_hours") or {}
    if not q.get("enabled"):
        return False
    h = now_kst().hour
    start, end = int(q.get("start", 1)), int(q.get("end", 7))
    return start <= h < end if start < end else (h >= start or h < end)


def messages_due(args, store: Store) -> bool:
    """이 실행이 수신 메시지를 처리해야 하는가.

    평소에는 1분마다 도는 전담 실행(messages.yml)이 전부 가져간다.
    두 실행이 같은 순간에 텔레그램을 읽으면 같은 링크에 두 번 답할 수 있어서,
    5분짜리 실행은 전담이 조용해졌을 때만 끼어든다.
    """
    if args.messages_only or args.messages_if_stale <= 0:
        return True
    last = float(store.data.get("last_msg_poll") or 0)
    quiet = (time.time() - last) / 60
    if quiet <= args.messages_if_stale:
        return False
    log.warning("메시지 전담 실행이 %.0f분째 조용합니다 — 이번 실행이 대신 처리합니다 "
                "(앱스 스크립트 1분 트리거를 확인하세요)", quiet)
    return True


def weekly_status(store: Store, cfg: dict) -> str:
    """/status 에 한 줄로 보여줄 주간 리포트 상태."""
    if not (cfg.get("weekly") or {}).get("enabled", True):
        return "꺼짐"
    due = weekly.deadline(cfg)
    if float(store.data.get("last_weekly") or 0) >= due.timestamp():
        nxt = due + timedelta(days=7)
        return f"{due:%m/%d} 발송 완료 · 다음 {nxt:%m/%d} {nxt.hour}시"
    return f"{due:%m/%d %H시} 마감분 대기 중"


def summarize_and_render(sm: Summarizer, item: dict, cfg: dict, mode: str,
                         body_override: str | None = None):
    """기사 → (텔레그램 메시지, 카드). 버려야 하면 (None, 카드).

    body_override: 텔레그램에서 '전달'된 글처럼 본문을 이미 손에 쥔 경우.
    링크를 열지 않고 그 글을 그대로 요약한다.
    """
    limits = cfg.get("limits") or {}
    if body_override is not None:
        card = sm.run(item, body_override[: limits.get("article_max_chars", 12000)],
                      mode=mode, body_ok=True)
        if not card:
            return None, None
        card["_body_ok"] = True
        return tg.render(card, item), card

    url = extract.resolve(item["link"])
    item = dict(item, link=url)

    body, ok, fetched = extract.article_text(url, limits.get("article_max_chars", 12000))
    if fetched and not item.get("title"):
        # 링크만 보냈을 때는 제목이 비어 있다. 페이지 제목을 채워 넣어야
        # 모델이 본문과 제목의 어긋남을 알아챌 수 있다.
        item = dict(item, title=fetched)
    if not ok and not body:
        body = extract.clean_html(item.get("summary", ""))

    card = sm.run(item, body, mode=mode, body_ok=ok)
    if not card:
        return None, None
    # 본문을 제대로 못 읽었으면 카드가 얕거나 엉뚱할 수 있다. 호출 측이
    # 알 수 있게 표시해 둔다.
    card["_body_ok"] = ok
    if mode == "feed":
        if not card.get("publish"):
            log.info("모델이 버림 [%s] %s", card.get("reject_reason", "")[:40], item["title"][:50])
            return None, card
        if int(card.get("importance", 0)) < limits.get("min_importance", 3):
            log.info("중요도 %s 미달: %s", card.get("importance"), item["title"][:50])
            return None, card
    return tg.render(card, item), card


# ---------------------------------------------------------------- 모드 C

def handle_messages(bot: tg.Telegram, store: Store, sm: Summarizer, cfg: dict) -> None:
    updates = bot.get_updates(store.tg_offset)
    if not updates:
        return

    for u in updates:
        store.tg_offset = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != bot.chat_id:
            continue  # 남이 보낸 건 무시
        # 사진·파일을 전달하면 본문이 caption 에 들어 있다
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text:
            continue

        cmd = text.split()[0].lower().split("@")[0]
        if cmd in ("/start", "/help"):
            bot.send(HELP)
            continue
        if cmd == "/status":
            d = store.data
            bot.send(
                f"오늘 발행: <b>{d['posted_today']}</b>건 / 상한 "
                f"{(cfg.get('limits') or {}).get('max_posts_per_day', 6)}건\n"
                f"대기 큐: {len(d['queue'])}건\n"
                f"자동 발행: {'⏸ 중지' if store.paused else '▶️ 작동중'}\n"
                f"누적 발행: {d['stats'].get('total_posted', 0)}건\n"
                f"마지막 실행: {d['stats'].get('last_run', '-')}\n"
                f"주간 리포트: {weekly_status(store, cfg)}\n"
                f"옵시디언 저장: {'켜짐' if vault.enabled() else '꺼짐 (VAULT_REPO/VAULT_TOKEN 없음)'}"
            )
            continue
        if cmd == "/pause":
            store.paused = True
            bot.send("⏸ 자동 발행을 멈췄습니다. 링크를 보내면 정리는 계속 해드려요.")
            continue
        if cmd == "/resume":
            store.paused = False
            bot.send("▶️ 자동 발행을 재개합니다.")
            continue

        handle_input(bot, store, sm, cfg, msg, text)


def handle_input(bot: tg.Telegram, store: Store, sm: Summarizer, cfg: dict,
                 msg: dict, text: str) -> None:
    """DM 에 들어온 링크·전달글·메모 하나를 처리한다.

    텔레그램 📝 기사 요약 채널로 카드를 보내는 것은 예전과 같고, 볼트가 설정돼
    있으면 같은 카드를 옵시디언 아카이브에도 저장한다.
    """
    origin, fwd_link = archive.forward_info(msg)
    tags, memo = archive.parse(text)
    url = extract.find_url(text)
    kind, folder = archive.route(tags)

    # 0) 일기 — 저녁 질문(📓)에 답장했거나, #일기 를 붙였거나, 질문 뒤 새벽 4시 전까지 보낸 그냥 글
    replied = ((msg.get("reply_to_message") or {}).get("text") or "").startswith("📓")
    window = time.time() < float(store.data.get("diary_open_until") or 0)
    if not origin and ("일기" in tags or replied or (window and not url and not tags)):
        path = archive.save_diary(memo if "일기" in tags else text)
        bot.send("📓 오늘 일기에 적어뒀어요." if path else "⚠️ 일기 저장 실패 (볼트 설정 확인: /status)")
        return

    # 1) 개인 태그 (#여행 #일정 #월급) — 요약하지 않고 그대로 쌓는다
    if kind == "personal":
        path = archive.save_personal(folder, text, url or fwd_link, memo if not origin else text[:200])
        bot.send(f"📥 {folder}에 저장했습니다." if path else "⚠️ 옵시디언 저장 실패 (볼트 설정 확인: /status)")
        return

    # 2) 전달된 글 — 본문이 메시지에 있으므로 링크를 열 필요가 없다.
    #    단, 짧은 전달글(링크 한 줄 + 코멘트)은 그 링크를 여는 편이 낫다.
    if origin and (len(memo) >= 150 or not url):
        log.info("전달글 요청: %s (%d자)", origin, len(text))
        bot.send("⏳ 정리 중…" if len(text) < LONG_CHARS else "⏳ 긴 글이라 정리본까지 만드는 중… (1분 정도)")
        first = memo.split("。")[0].split(". ")[0][:80]
        item = {"title": first, "link": fwd_link or url or "", "source": origin,
                "published": None, "summary": ""}
        rendered, card = summarize_and_render(sm, item, cfg, mode="ondemand", body_override=text)
        long = sm.run_long(item, text) if rendered and len(text) >= LONG_CHARS else None
        _deliver(bot, store, rendered, card, item, tags, memo="", origin=origin,
                 src_type="텔레그램", mark_url=None, long=long, raw=text if long else None)
        return

    # 3-1) 블로그 — 텔레그램엔 같은 짧은 카드, 위키엔 긴 정리본 + 원문 사본
    if url and vault.source_type(url) == "블로그":
        if handle_blog(bot, store, sm, cfg, url, tags, memo):
            return

    # 3) 링크 — 예전과 같은 요약 + 볼트 저장
    if url:
        log.info("온디맨드 요청: %s", url)
        bot.send("⏳ 정리 중…")
        item = {"title": "", "link": url, "source": "", "published": None,
                "summary": "", "memo": memo}
        rendered, card = summarize_and_render(sm, item, cfg, mode="ondemand")
        _deliver(bot, store, rendered, card, dict(item, link=extract.resolve(url)), tags,
                 memo=memo, origin=origin, src_type="", mark_url=url)
        return

    # 4) 링크도 전달도 아닌 그냥 메모
    path = archive.save_memo(text, tags)
    bot.send("📝 메모로 저장했습니다. (밤 정리 때 자리를 찾아 넣습니다)" if path else
             "링크를 찾지 못했습니다. 기사 URL을 포함해서 보내주세요. (/help)")


LONG_CHARS = 1500     # 이보다 긴 전달글은 블로그처럼 긴 정리본도 만든다
BLOG_MAX_CHARS = 40000


def handle_blog(bot, store, sm, cfg, url, tags, memo) -> bool:
    """블로그 링크. 본문을 못 읽으면 False 를 돌려 일반 링크 처리로 넘긴다."""
    murl = extract.blog_url(url)
    log.info("블로그 요청: %s", murl)
    bot.send("⏳ 블로그라 정리본까지 만드는 중… (1분 정도)")
    body, ok, title = extract.article_text(murl, BLOG_MAX_CHARS)
    if not ok:
        log.info("블로그 본문 추출 실패 — 일반 링크로 처리: %s", murl)
        return False
    item = {"title": title, "link": url, "source": "", "published": None,
            "summary": "", "memo": memo}
    limit = (cfg.get("limits") or {}).get("article_max_chars", 12000)
    rendered, card = summarize_and_render(sm, item, cfg, mode="ondemand", body_override=body[:limit])
    long = sm.run_long(item, body) if rendered else None
    _deliver(bot, store, rendered, card, item, tags, memo=memo, origin="",
             src_type="블로그", mark_url=url, long=long, raw=body)
    return True


def _deliver(bot, store, rendered, card, item, tags, memo, origin, src_type, mark_url,
             long: dict | None = None, raw: str | None = None) -> None:
    if not rendered:
        bot.send("❌ 정리 실패. 본문을 못 읽었거나(유료 기사) 모델 호출이 실패했습니다.")
        if vault.enabled() and (item.get("link") or memo):
            archive.save_memo(f"(요약 실패) {item.get('link', '')} {memo}", tags)
        return
    # 요약 카드는 전용 채널에 쌓고, DM 은 입력창으로만 쓴다.
    # 채널을 아직 안 만들었으면(값 없음) 여기 DM 으로 그대로 보낸다 —
    # 없는 채널을 가리키며 '올렸습니다' 라고 거짓말하지 않기 위해서다.
    to = "summary" if bot.channels.get("summary") else None
    bot.send(rendered, channel=to)

    _, sector = archive.route(tags, (long or card).get("sector", ""))
    if long:
        raw_path = archive.save_raw(sector, long.get("title") or card.get("title", ""),
                                    item.get("link", ""), origin, raw or "")
        saved = archive.save_long_card(long, item.get("link", ""), sector, tags, memo,
                                       origin=origin, src_type=src_type, raw_path=raw_path)
    else:
        saved = archive.save_card(card, item.get("link", ""), sector, tags, memo,
                                  origin=origin, src_type=src_type)

    if saved:
        count_saved(store, sector)

    notes = []
    if to:
        notes.append("✅ 📝 기사 요약 채널에 올렸습니다.")
    if saved:
        notes.append(f"🗂 옵시디언 <b>{sector}</b> 아카이브에 "
                     + ("장문 정리본 + 원문 사본을 저장했습니다." if long else "저장했습니다."))
    elif vault.enabled():
        notes.append("⚠️ 옵시디언 저장 실패 — 요약 채널에는 올라갔습니다.")
    if not card.get("_body_ok"):
        notes.append(
            "⚠️ 본문을 제대로 못 읽었습니다 (유료 기사·봇 차단·삭제된 페이지, "
            "또는 관련뉴스 카드만 잡힌 경우). 카드가 얕거나 제목과 어긋나면 "
            "이 때문이니 원문을 한 번 확인해 주세요."
        )
    if notes:
        bot.send("\n".join(notes))
    if mark_url:
        store.mark(mark_url, "")  # 같은 기사가 자동 피드로 또 오지 않게


# ---------------------------------------------------------------- 저녁 일기 질문

def run_diary_ask(bot: tg.Telegram, store: Store, cfg: dict) -> None:
    """매일 저녁 한 번 DM 으로 '오늘 뭐 했어?' 를 묻는다. 답은 handle_input 이 일기로 받는다."""
    d = cfg.get("diary") or {}
    if not d.get("enabled", True) or not vault.enabled():
        return
    now = now_kst()
    today = now.strftime("%Y-%m-%d")
    if store.data.get("diary_asked") == today:
        return
    if (now.hour, now.minute) < (int(d.get("hour", 21)), int(d.get("minute", 30))):
        return
    store.data["diary_asked"] = today
    until = (now + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
    store.data["diary_open_until"] = until.timestamp()

    lines = [f"📓 <b>{now:%m/%d} 오늘 하루 어땠어요?</b>", "",
             "한 일, 떠오른 생각, 내일 할 일을 편하게 답장으로 보내주세요.",
             "여러 번 나눠 보내도 되고, 새벽 4시까지 받아요. 오늘은 건너뛰려면 그냥 두면 돼요."]
    saved = store.data.get("diary_saved_today") or {}
    if saved.get("day") == today and saved.get("n"):
        by = " · ".join(f"{k} {v}" for k, v in sorted(saved["by"].items(), key=lambda x: -x[1]))
        lines += ["", f"오늘 위키에 저장된 카드: <b>{saved['n']}</b>건 ({by})"]
    bot.send("\n".join(lines))
    log.info("저녁 일기 질문 발송")


def count_saved(store: Store, sector: str) -> None:
    """저녁 질문에 '오늘 저장한 카드' 를 보여주려고 섹터별로 센다."""
    today = now_kst().strftime("%Y-%m-%d")
    s = store.data.get("diary_saved_today") or {}
    if s.get("day") != today:
        s = {"day": today, "n": 0, "by": {}}
    s["n"] += 1
    s["by"][sector] = s["by"].get(sector, 0) + 1
    store.data["diary_saved_today"] = s


# ---------------------------------------------------------------- 모드 A+B

def flush_queue(bot: tg.Telegram, store: Store, cfg: dict) -> None:
    """조용한 시간에 쌓아둔 것을 아침에 내보낸다.

    일일 한도는 큐에 넣는 시점에 이미 차감했으므로 여기서는 다시 세지 않는다.
    """
    if in_quiet_hours(cfg) or not store.data["queue"]:
        return
    queued = store.drain_queue()
    log.info("밀린 %d건 발송", len(queued))
    bot.send(f"🌅 밤사이 모아둔 뉴스 <b>{len(queued)}</b>건입니다.")
    for payload in queued:
        bot.send(payload["text"])


def render_alert(item: dict, m: dict) -> str:
    """알람 한 건. AI를 안 쓰므로 제목을 그대로 옮기고 왜 울렸는지만 덧붙인다."""
    tag = m["rule"]
    if m["company"]:
        tag = m["company"] + " · " + m["rule"]
    foot = [x for x in (item.get("source") or "",) if x]
    if item.get("published"):
        foot.append(datetime.fromtimestamp(item["published"]).strftime("%H:%M"))
    why = " ".join(m["words"][:3])

    return NEWLINE.join([
        "⚡ <b>" + tg._esc(tag) + "</b>",
        "",
        '<a href="' + tg._esc(item["link"]) + '">' + tg._esc(item["title"]) + "</a>",
        "",
        "<i>" + tg._esc(" · ".join(foot)) + "</i>  ·  <code>" + tg._esc(why) + "</code>",
    ])


def run_alerts(bot: tg.Telegram, store: Store, cfg: dict, items: list[dict]) -> int:
    a = cfg.get("alerts") or {}
    if not a.get("enabled", True):
        return 0
    if quiet_for(cfg, "alert"):
        log.info("조용한 시간 — 알람 건너뜀")
        return 0
    budget = min(a.get("max_per_run", 5), store.alerts_left_today(a.get("max_per_day", 30)))
    if budget <= 0:
        log.info("오늘 알람 한도 소진")
        return 0

    sent = 0
    for item, m in alerts.pick(items, cfg, store, budget):
        store.mark_alerted(item["link"], item["title"])
        store.touch_cooldown(m["company"], m["rule"])
        if bot.send(render_alert(item, m), channel="alert"):
            sent += 1
            log.info("알람 [%s/%s] %s", m["rule"], m["company"] or "-", item["title"][:44])
    return sent


def quiet_for(cfg: dict, what: str) -> bool:
    """지금이 이 모드의 조용한 시간인가.

    수집은 계속하고 발송만 멈춘다. 새벽에 쌓인 것은 아침 첫 실행 때
    한 번에 나가므로, 아침 다이제스트와 같은 효과를 공짜로 낸다.
    """
    q = cfg.get("quiet_hours") or {}
    return what in (q.get("applies_to") or []) and in_quiet_hours(cfg)


def run_clipping(bot: tg.Telegram, store: Store, cfg: dict, items: list[dict]) -> int:
    c = cfg.get("clipping") or {}
    if not c.get("enabled", True):
        return 0

    added = clipping.collect(items, cfg, store)
    if added:
        log.info("클리핑 버퍼에 %d건 추가 (누적 %d건)", added, len(store.data["clip_buffer"]))

    if quiet_for(cfg, "clip"):
        log.info("조용한 시간 — 클리핑은 모아뒀다가 아침에 보냅니다 (%d건)",
                 len(store.data["clip_buffer"]))
        return 0
    if not store.clip_due(c.get("interval_hours", 2)):
        return 0
    if len(store.data["clip_buffer"]) < c.get("min_items", 3):
        log.info("클리핑 건수 부족(%d건) — 다음 회차로", len(store.data["clip_buffer"]))
        return 0

    rows = store.flush_clip()
    alerted = set(store.data["alerted"])
    alerted_urls = {r["u"] for r in rows if url_key(r["u"]) in alerted}
    chunks = clipping.render(rows, cfg, alerted_urls)
    for chunk in chunks:
        bot.send(chunk, channel="clip")
    log.info("클리핑 발송 (버퍼 %d건 중 상한만큼)", len(rows))
    return len(rows)


def run_weekly(bot: tg.Telegram, store: Store, cfg: dict, force: bool = False) -> int:
    """부동산 주간 News Flow. 금요일 08시가 지난 첫 실행에서 한 번만 나간다.

    워크플로를 따로 두지 않는다. GitHub 예약은 몇 시간씩 밀리는데(README 참고)
    이 봇은 앱스 스크립트가 5분마다 제 시각에 깨워주므로, 그 실행에 얹는 편이
    금요일 08시를 훨씬 잘 지킨다.
    """
    if not force and quiet_for(cfg, "clip"):
        # 새벽에 밀린 실행이 걸린 경우다. 도장을 안 찍었으니 07시 이후 첫 실행에 나간다.
        log.info("조용한 시간 — 주간 리포트는 아침으로 미룹니다")
        return 0
    return weekly.run(bot, store, cfg, force=force)


def run_news_cycle(bot: tg.Telegram, store: Store, sm: Summarizer, cfg: dict,
                   items: list[dict] | None = None) -> None:
    limits = cfg.get("limits") or {}
    quiet = in_quiet_hours(cfg)

    budget = min(
        limits.get("max_posts_per_run", 3),
        store.remaining_today(limits.get("max_posts_per_day", 6)),
    )
    if quiet:
        # 미국 뉴스는 한국 새벽에 몰린다. 야간이 하루 한도를 다 먹어버리면
        # 정작 낮에 나오는 국내 뉴스가 막히므로 밤 몫을 따로 제한한다.
        night_cap = limits.get("max_night_queue", 3)
        budget = min(budget, max(0, night_cap - len(store.data["queue"])))
    if budget <= 0:
        log.info("발행 한도 소진 (야간 몫)" if quiet else "오늘 발행 한도 소진")
        return

    items = items if items is not None else sources.collect(cfg)
    fresh = [i for i in items if store.is_new(i["link"], i["title"])]
    candidates = scoring.shortlist(fresh, cfg)
    log.info("신규 %d건 → 후보 %d건 (예산 %d건%s)",
             len(fresh), len(candidates), budget, ", 야간" if quiet else "")
    sent = 0
    for item in candidates:
        if sent >= budget:
            break
        # 같은 사건이 여러 매체에서 후보로 올라온다. 점수순이라 앞의 것이 원문/상세 기사고,
        # 뒤따르는 재탕 기사는 여기서 걸린다.
        if not store.is_new(item["link"], item["title"]):
            log.info("중복 건너뜀: %s", item["title"][:50])
            continue

        # 같은 회사 건을 하루에 여러 번 보내지 않는다. 대형 수주는 공시가 쪼개져
        # 나오거나 매체마다 각도가 달라서, 제목만으로는 같은 사건인 줄 모른다.
        firms = scoring.companies_in(item["title"], cfg)
        if firms and not store.company_quota_left(firms, limits.get("max_per_company_per_day", 1)):
            log.info("오늘 %s 건은 이미 보냈음: %s", "/".join(firms[:2]), item["title"][:44])
            continue

        store.mark(item["link"], item["title"])  # 판정만 해도 기록 (재시도 방지)

        rendered, card = summarize_and_render(sm, item, cfg, mode="feed")
        if not rendered:
            continue

        # 조용한 시간엔 중요도 5만 즉시, 나머지는 아침으로.
        # 큐에 넣는 것도 발행 예산을 쓴다 — 안 그러면 밤새 한도를 우회해 쌓인다.
        if quiet and int(card.get("importance", 0)) < 5:
            store.enqueue({"text": rendered})
            store.count_post(firms)
            sent += 1
            log.info("큐에 보관 (중요도 %s): %s", card.get("importance"), card["title"][:40])
            continue

        if bot.send(rendered):
            store.count_post(firms)
            sent += 1
            log.info("발행 (중요도 %s): %s", card.get("importance"), card["title"][:40])


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--state", default=str(ROOT / "state" / "state.json"))
    ap.add_argument("--messages-only", action="store_true",
                    help="메시지 응답만 하고 뉴스 수집은 건너뜀 (worker 용)")
    ap.add_argument("--no-messages", action="store_true",
                    help="뉴스 수집만 하고 수신 메시지는 건드리지 않음. "
                         "worker 가 상시 대기 중일 때 같은 메시지에 두 번 답하는 것을 막는다.")
    ap.add_argument("--messages-if-stale", type=float, default=0, metavar="분",
                    help="메시지 전담 실행(--messages-only)이 이 시간(분) 넘게 "
                         "조용하면 이 실행이 대신 메시지를 처리한다. 0 이면 항상 처리.")
    ap.add_argument("--weekly-now", action="store_true",
                    help="부동산 주간 News Flow 를 지금 당장 한 번 만든다 "
                         "(요일·마감 무시. --dry-run 과 같이 쓰면 콘솔로만 확인)")
    ap.add_argument("--dry-run", action="store_true",
                    help="텔레그램 발송 없이 콘솔에만 출력")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    n = load_dotenv(ROOT / ".env")
    if n:
        log.info(".env 에서 %d개 값을 읽었습니다", n)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY 가 없습니다. .env 파일에 넣거나 환경변수로 설정하세요.")
        return 1

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        # --dry-run 은 콘솔에만 출력하므로 텔레그램 없이도 요약 품질을 볼 수 있다
        if not args.dry_run:
            log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없습니다.")
            return 1
        log.info("텔레그램 설정 없음 — 콘솔 출력만 하고 수신 메시지는 건너뜁니다")
        token, chat_id = "dry-run", "dry-run"
        has_telegram = False
    else:
        has_telegram = True

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    store = Store(args.state)
    sm = Summarizer(cfg)

    # 채널 배정: config 에 적힌 환경변수 이름으로 실제 chat_id 를 찾는다.
    # 값이 없으면 개인 DM 으로 떨어지므로, 채널을 아직 안 만들었어도 메시지가 사라지지 않는다.
    ch_cfg = cfg.get("channels") or {}
    channels = {name: os.environ.get(envname, "") for name, envname in ch_cfg.items()}
    for name, cid in channels.items():
        log.info("채널 %-5s -> %s", name, cid or "(미설정 · 개인 DM)")

    bot = tg.Telegram(token, chat_id, channels)
    if args.dry_run:
        def _show(text, preview=False, channel=None, plain=False):
            print(NEWLINE + "=" * 64 + "  [" + (channel or "DM") + "]" + NEWLINE + text)
            return True
        bot.send = _show

    try:
        if args.weekly_now:
            run_weekly(bot, store, cfg, force=True)
            return 0
        if has_telegram and not args.no_messages and messages_due(args, store):
            handle_messages(bot, store, sm, cfg)
            if args.messages_only:
                # 전담 실행만 도장을 찍는다. 대타가 찍으면 20분마다 한 번씩만
                # 처리되는 이상한 리듬이 된다.
                store.data["last_msg_poll"] = int(time.time())
        if not args.messages_only:
            flush_queue(bot, store, cfg)
            if store.paused:
                log.info("일시정지 상태 — 자동 발행 건너뜀")
            else:
                # 피드는 한 번만 읽고 알람·클리핑·(선택)AI선별이 나눠 쓴다
                # 알람·클리핑·AI선별이 전부 꺼져 있으면 피드를 읽을 이유가 없다
                # (주간 리포트는 자기 소스를 따로 읽는다)
                need_feeds = any((cfg.get(k) or {}).get("enabled", d) for k, d in
                                 (("alerts", True), ("clipping", True), ("curation", False)))
                items = sources.collect(cfg) if need_feeds else []
                run_alerts(bot, store, cfg, items)
                run_clipping(bot, store, cfg, items)
                run_weekly(bot, store, cfg)
                run_diary_ask(bot, store, cfg)
                if (cfg.get("curation") or {}).get("enabled", False):
                    run_news_cycle(bot, store, sm, cfg, items)
    finally:
        # 연습 실행이 상태를 더럽히면, 정작 실제 실행 때 그 기사들이 이미 본 것으로 처리된다
        if args.dry_run:
            log.info("연습 실행이라 상태를 저장하지 않습니다")
        else:
            store.save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
