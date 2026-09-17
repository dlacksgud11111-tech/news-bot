"""클리핑 — 훑어보기용 목록. AI를 쓰지 않는다.

한 번 돌 때마다 관심 범위에 드는 기사를 버퍼에 쌓아두고,
정해진 시간이 되면 섹터별로 묶어 한 번에 보낸다.

요약하지 않는 이유: 요약은 사람이 고른 뒤에 해야 값어치가 있다.
전부 요약하면 돈이 들고 느려지는데, 정작 읽는 건 몇 건 안 된다.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from scoring import _contains, companies_in, score
from store import is_same_story

UNCLASSIFIED = "기타"


def sector_of(item: dict, cfg: dict) -> str | None:
    """기사를 섹터 하나로 분류한다. 어디에도 안 걸리면 None (클리핑 제외).

    제목만 본다. 본문까지 보면 '햇빛지도 공개'나 '온수기 수상' 같은 기사도
    본문에 '배전'이 한 번 스쳤다는 이유로 전력기기에 들어온다.

    여러 섹터에 걸리면 config 에 적힌 순서가 빠른 쪽을 쓴다 —
    전력기기를 위에 두면 '원전 변압기 수주'가 전력기기로 간다.
    """
    sectors = cfg.get("sectors") or {}
    title = item["title"].lower()
    for name, words in sectors.items():
        if any(_contains(title, w) for w in words):
            return name
    return None


def collect(items: list[dict], cfg: dict, store) -> int:
    """이번 실행에서 새로 본 기사를 클리핑 버퍼에 쌓는다. 쌓은 개수를 돌려준다."""
    blocklist = cfg.get("blocklist") or []
    added = 0
    cap = (cfg.get("clipping") or {}).get("buffer_cap", 200)

    for item in items:
        if len(store.data["clip_buffer"]) >= cap:
            break
        if store.already_clipped(item["link"], item["title"]):
            continue
        title = item["title"].lower()
        if any(_contains(title, b) for b in blocklist):
            store.mark_clipped(item["link"], item["title"])  # 다시 검사하지 않도록
            continue
        sector = sector_of(item, cfg)
        if not sector:
            store.mark_clipped(item["link"], item["title"])
            continue

        store.mark_clipped(item["link"], item["title"])

        # 큰 건은 매체 10곳이 같이 쓴다. 목록에서는 한 줄로 묶고 몇 곳이 썼는지만 센다.
        dup = next((r for r in store.data["clip_buffer"]
                    if r["sec"] == sector and is_same_story(item["title"], r["t"])), None)
        if dup:
            dup["n"] = dup.get("n", 1) + 1
            continue

        sc, _ = score(item, cfg)
        store.data["clip_buffer"].append({
            "t": item["title"][:200],
            "u": item["link"],
            "s": (item.get("source") or "")[:40],
            "sec": sector,
            "ts": int(item.get("published") or 0),
            "sc": sc,
            "n": 1,
            "co": (companies_in(item["title"], cfg) or [""])[0],
        })
        added += 1
    return added


def _esc(s: str) -> str:
    return html.escape(str(s or "").strip(), quote=False)


def render(buffer: list[dict], cfg: dict, alerted_urls: set[str]) -> list[str]:
    """버퍼 → 텔레그램 HTML 메시지. 섹터별로 묶고 최신순."""
    c = cfg.get("clipping") or {}
    per_sector = c.get("max_per_sector", 8)
    total_cap = c.get("max_items", 40)

    order = list((cfg.get("sectors") or {}).keys())
    groups: dict[str, list[dict]] = {}
    for row in buffer:
        groups.setdefault(row["sec"], []).append(row)

    # 관련도 높은 것부터. 같은 점수면 최신 것부터.
    # 한 회사가 섹터를 독차지하지 않게 한다. 큰 수주가 터진 날이면 매체마다
    # 각도를 달리해 열 꼭지씩 쓰는데, 제목이 서로 달라 같은 사건으로 안 묶인다.
    per_company = c.get("max_per_company", 3)
    kept, dropped = {}, 0
    for sec, rows in groups.items():
        rows.sort(key=lambda r: (r.get("sc", 0), r.get("ts") or 0), reverse=True)
        picked, by_co = [], {}
        for r in rows:
            co = r.get("co") or ""
            if co and by_co.get(co, 0) >= per_company:
                continue
            picked.append(r)
            if co:
                by_co[co] = by_co.get(co, 0) + 1
            if len(picked) >= per_sector:
                break
        kept[sec] = picked
        dropped += max(0, len(rows) - len(picked))

    shown = sum(len(v) for v in kept.values())
    if shown > total_cap:  # 그래도 많으면 점수 낮은 섹터 꼬리부터 더 자른다
        flat = sorted((r for v in kept.values() for r in v),
                      key=lambda r: (r.get("sc", 0), r.get("ts") or 0), reverse=True)[:total_cap]
        keepset = {id(r) for r in flat}
        for sec in kept:
            kept[sec] = [r for r in kept[sec] if id(r) in keepset]
        dropped += shown - total_cap
        shown = total_cap

    now = datetime.now().strftime("%m/%d %H:%M")
    head = f"<b>📰 전력·에너지 클리핑</b>  {now}  ·  {shown}건"
    if dropped:
        head += f" <i>(관련도 낮은 {dropped}건 제외)</i>"
    lines = [head, ""]

    for sec in order:
        rows = kept.get(sec)
        if not rows:
            continue
        lines.append(f"<b>── {_esc(sec)} ({len(rows)})</b>")
        for r in rows:
            mark = "🚨 " if r["u"] in alerted_urls else ""
            src = _esc(r.get("s") or "")
            if r.get("n", 1) > 1:
                src += f" 외 {r['n'] - 1}곳"
            src = f" <i>{src}</i>" if src else ""
            lines.append(f'· {mark}<a href="{_esc(r["u"])}">{_esc(r["t"])}</a>{src}')
        lines.append("")

    lines.append("<i>훑어보고 필요한 건 링크를 봇에게 보내면 정리해 드립니다.</i>")
    return ["\n".join(lines)]
