"""엔트리포인트.

한 번 실행될 때 하는 일:
  1) 내가 봇에게 보낸 메시지 처리 (링크 정리 / 명령어)   ← 모드 C
  2) 조용한 시간에 밀렸던 발행물 내보내기
  3) 뉴스 수집 → 선별 → 요약 → 발송                      ← 모드 A+B
  4) 상태 저장
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml

from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

import alerts  # noqa: E402
import clipping  # noqa: E402
import extract  # noqa: E402
import scoring  # noqa: E402
import sources  # noqa: E402
import tg  # noqa: E402
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


HELP = """<b>전력·에너지 뉴스 봇</b>

기사 링크를 그냥 보내면 번역·요약해서 정리해 드립니다.

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


def summarize_and_render(sm: Summarizer, item: dict, cfg: dict, mode: str):
    """기사 → (텔레그램 메시지, 카드). 버려야 하면 (None, 카드)."""
    limits = cfg.get("limits") or {}
    url = extract.resolve(item["link"])
    item = dict(item, link=url)

    body, ok = extract.article_text(url, limits.get("article_max_chars", 12000))
    if not ok and not body:
        body = extract.clean_html(item.get("summary", ""))

    card = sm.run(item, body, mode=mode)
    if not card:
        return None, None
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
        text = (msg.get("text") or "").strip()
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
                f"마지막 실행: {d['stats'].get('last_run', '-')}"
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

        url = extract.find_url(text)
        if not url:
            bot.send("링크를 찾지 못했습니다. 기사 URL을 포함해서 보내주세요. (/help)")
            continue

        log.info("온디맨드 요청: %s", url)
        bot.send("⏳ 정리 중…")
        item = {"title": "", "link": url, "source": "", "published": None, "summary": ""}
        rendered, _ = summarize_and_render(sm, item, cfg, mode="ondemand")
        if rendered:
            bot.send(rendered)
            store.mark(url, "")  # 같은 기사가 자동 피드로 또 오지 않게
        else:
            bot.send("❌ 정리 실패. 본문을 못 읽었거나(유료 기사) 모델 호출이 실패했습니다.")


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
        def _show(text, preview=False, channel=None):
            print(NEWLINE + "=" * 64 + "  [" + (channel or "DM") + "]" + NEWLINE + text)
            return True
        bot.send = _show

    try:
        if has_telegram and not args.no_messages:
            handle_messages(bot, store, sm, cfg)
        if not args.messages_only:
            flush_queue(bot, store, cfg)
            if store.paused:
                log.info("일시정지 상태 — 자동 발행 건너뜀")
            else:
                # 피드는 한 번만 읽고 알람·클리핑·(선택)AI선별이 나눠 쓴다
                items = sources.collect(cfg)
                run_alerts(bot, store, cfg, items)
                run_clipping(bot, store, cfg, items)
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
