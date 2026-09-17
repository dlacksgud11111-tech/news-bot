"""부동산 주간 News Flow — 금요일 오전에 한 번, 8건.

LS증권 '부동산 Weekly Data' 의 '지난주 주요 News Flow' 칸에 그대로 붙일 수 있는
두 줄 형식으로 뽑는다. 여기서도 AI 는 쓰지 않는다 — 선정 기준이 이미 말로
정해져 있어서(가격동향 최소 1건, 매체당 2건, 같은 이슈는 대표 1건) 규칙으로
옮기는 편이 결과가 일정하다.

대상 기간은 지난주 금요일 00:00 ~ 이번주 금요일 08:00 (KST).
실행할 때마다 그 창을 다시 계산하므로, 금요일 아침 실행이 밀려서 토요일에
돌아도 같은 주를 채워 보낸다.

같은 이슈 판정은 store.is_same_story 를 쓰지 않고 여기서 따로 한다.
그쪽은 '1665억 vs 2200억' 처럼 숫자가 사건을 가르는 전력기기 공시용이고,
부동산 기사는 '서울 아파트값 84주 연속 상승' 을 매체 15곳이 제목만 바꿔 쓰는
쪽이라 글자 겹침(bigram)을 기본으로 삼고, 제목 속 숫자와 맨 앞 주체를
보조 지문으로 쓴다.
"""

from __future__ import annotations

import calendar
import html
import logging
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import feedparser
import requests

import extract
from clipping import display_title
from scoring import _contains
from sources import HEADERS, google_news_url
from store import KST

log = logging.getLogger(__name__)

WEEKDAY_KO = "월화수목금토일"
NEWLINE = chr(10)


# ---------------------------------------------------------------- 기간

def deadline(cfg: dict, now: datetime | None = None) -> datetime:
    """가장 최근에 지나간 마감 시각 (기본: 금요일 08:00 KST).

    이 값이 '이번 주 리포트의 끝'이고, 동시에 중복 발송을 막는 도장이다.
    """
    w = cfg.get("weekly") or {}
    weekday = int(w.get("weekday", 5))   # ISO 기준 1=월 … 7=일
    hour = int(w.get("hour", 8))
    now = now or datetime.now(KST)

    d = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    d -= timedelta(days=(d.isoweekday() - weekday) % 7)
    if d > now:
        d -= timedelta(days=7)
    return d


def window(cfg: dict, now: datetime | None = None) -> tuple[datetime, datetime]:
    end = deadline(cfg, now)
    return (end - timedelta(days=7)).replace(hour=0), end


def _fmt_day(d: datetime) -> str:
    return f"{d:%y}/{d.month}/{d.day}({WEEKDAY_KO[d.weekday()]})"


# ---------------------------------------------------------------- 수집

def _fetch(query: str, window_days: int, timeout: int = 20) -> list[dict]:
    """구글 뉴스 검색 한 건. 기간은 넉넉히 받아온 뒤 pubDate 로 다시 거른다."""
    url = google_news_url(f"{query} when:{window_days}d", "ko", "KR", "KR:ko")
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("주간 검색 실패 '%s': %s", query, e)
        return []

    out = []
    for e in feedparser.parse(r.content).entries:
        link = (e.get("link") or "").strip()
        title = (e.get("title") or "").strip()
        stamp = e.get("published_parsed") or e.get("updated_parsed")
        if not (link and title and stamp):
            continue
        src = e.get("source") or {}
        out.append({
            "title": title,
            "link": link,
            # published_parsed 는 UTC 기준 struct_time 이다. time.mktime 으로 읽으면
            # 실행 서버의 시간대만큼 밀려 기사 날짜가 하루 틀어진다 (리포트에 그대로 보인다).
            "dt": datetime.fromtimestamp(calendar.timegm(stamp), KST),
            "src_name": (src.get("title") or "").strip(),
            "src_url": src.get("href") or "",
            "q": query,
        })
    return out


def outlet_of(url: str, outlets: dict) -> str | None:
    """도메인 -> 매체명. 목록에 없는 도메인은 None (기본적으로 제외)."""
    host = urllib.parse.urlparse(url or "").netloc.lower()
    for dom in sorted(outlets, key=len, reverse=True):
        if host == dom or host.endswith("." + dom):
            return outlets[dom]
    return None


def _clean_title(title: str, src_name: str) -> str:
    """구글이 제목 끝에 붙이는 ' - 매체명' 을 뗀다."""
    t = html.unescape(title).strip()
    for _ in range(2):   # 구글은 '제목 - 머니투데이 - 머니투데이' 처럼 두 번 붙이기도 한다
        if src_name and t.endswith(" - " + src_name):
            t = t[: -(len(src_name) + 3)].strip()
    return display_title(t)


_AMP_PATH = re.compile(r"/amp(?=/|$)")


def strip_amp(url: str) -> str:
    """AMP 주소를 일반 주소로 되돌린다.

    /amp/view/123 -> /view/123,  articleViewAmp.html -> articleView.html
    (국내 언론사 CMS 에서 흔한 두 형태). 구글은 AMP 판을 자주 물려주는데,
    그게 리포트에 남으면 나중에 열었을 때 모바일 껍데기만 뜬다.
    """
    p = urllib.parse.urlsplit(url)
    path = _AMP_PATH.sub("", p.path)
    path = re.sub(r"Amp\.html$", ".html", path)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path or "/", p.query, p.fragment))


def bigrams(t: str) -> set[str]:
    s = re.sub(r"[^가-힣0-9A-Za-z]", "", t)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def sim(a: set, b: set) -> float:
    """짧은 쪽 기준 겹침. 제목 길이가 매체마다 달라 자카드보다 잘 맞는다."""
    return len(a & b) / max(1, min(len(a), len(b)))


def numbers(t: str) -> set[str]:
    """제목에 박힌 두 자리 이상 숫자. 사건을 특정하는 지문으로 쓴다."""
    out = set()
    for n in re.findall(r"\d[\d,]*", t):
        n = n.replace(",", "")
        if len(n) < 2:                                # 한 자리는 변별력이 없다
            continue
        if len(n) == 4 and 1900 <= int(n) <= 2100:    # 연도는 아무 기사에나 있다
            continue
        out.add(n)
    return out


_HEAD_TAG = re.compile(r"^\s*[\[\(【][^\]\)】]{0,14}[\]\)】]\s*")   # [속보] [단독] …
_SPLIT = re.compile(r"[\s,·…:‥]+")
# 따옴표는 유니코드로 적는다 — 정규식과 문자열에 그대로 섞으면 읽기 어려워진다.
_TRIM = "\u0022\u201c\u201d\u0027\u2019,·…:"
_JOSA = ("으로", "은", "는", "이", "가", "의", "도", "에", "를", "와", "과")


def subject(title: str) -> str:
    """제목 맨 앞의 주체. '홍지선 "용산공원…"' -> '홍지선'

    한국 기사 제목은 주체를 맨 앞에 놓는다. 같은 인사청문회 발언을 매체마다
    다른 대목으로 뽑아 쓰면 제목이 하나도 안 겹치는데, 앞머리는 늘 같다.
    """
    t = _HEAD_TAG.sub("", title)
    tok = next((x for x in _SPLIT.split(t) if x), "").strip(_TRIM)
    if len(tok) >= 3:
        for j in _JOSA:                      # '정부는' -> '정부'
            if tok.endswith(j) and len(tok) - len(j) >= 2:
                return tok[: -len(j)]
    return tok


# 이름 뒤에 흔히 붙는 말. 이게 뒤따르거나 이름이 따옴표·쉼표를 물고 있으면
# 세 글자 토큰을 사람 이름으로 본다. '공사비 5122억' 의 '공사비' 는 여기서 걸러진다.
_TITLE_WORDS = ("장관", "후보", "의원", "대표", "사장", "회장", "위원장", "차관", "청장",
                "시장", "지사", "부총리", "총리", "처장", "실장", "본부장", "교수",
                "원장", "국장", "소장", "팀장", "위원", "씨")
_QUOTE_HEAD = ("\u0022", "\u201c", "\u0027", "\u2018")
_HANGUL3 = re.compile(r"[가-힣]{3}")
# 세 글자여도 이름일 수 없는 꼬리 (용언·조사·행정구역)
_NOT_NAME = ("다", "요", "까", "등", "만", "의", "은", "는", "이", "가", "를", "에",
             "와", "과", "서", "로", "구", "시", "군", "동", "읍", "면")
# 두 음절 조사. '때까지' 처럼 세 글자 한글이어도 이름일 수 없다.
_NOT_NAME2 = ("까지", "부터", "보다", "처럼", "마다", "라도", "에서", "으로", "만큼",
              "이나", "대로", "밖에", "조차", "뿐만")


def name_stop(cfg: dict) -> set[str]:
    """이름으로 오해하면 안 되는 말들 — 검색어·카테고리 단어·일반 주체."""
    w = cfg.get("weekly") or {}
    stop = set(w.get("generic_subjects") or [])
    stop.update(w.get("queries") or [])
    for c in w.get("categories") or []:
        stop.update(c.get("words") or [])
    return stop


def person_of(title: str, stop: set[str]) -> str:
    """제목에 등장하는 사람 이름. 없으면 빈 문자열.

    인사청문회나 국정감사가 열리면 한 사람의 발언을 매체마다 다른 대목으로
    뽑아 쓴다. 제목이 하나도 안 겹치니 글자 겹침으로는 못 묶이는데,
    실제로는 같은 사건이라 그대로 두면 8칸의 절반을 한 사람이 먹는다.

    왼쪽부터 첫 후보를 쓰면 '시장 충분할 때까지… 홍지선 후보자, …' 에서
    '때까지' 를 집는다. 그래서 근거의 세기를 따져 따옴표·직함이 뒤따르는
    토큰(2)을 쉼표만 물고 있는 토큰(1)보다 앞세운다.
    """
    toks = _HEAD_TAG.sub("", title).split()
    best, best_strength = "", 0
    for i, raw in enumerate(toks):
        tok = raw.strip(_TRIM)
        if not _HANGUL3.fullmatch(tok) or tok in stop:
            continue
        if tok.endswith(_NOT_NAME) or tok.endswith(_NOT_NAME2):
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if nxt.startswith(_QUOTE_HEAD) or any(x in nxt for x in _TITLE_WORDS):
            strength = 2       # 홍지선 "용산공원…  /  홍지선 국토장관 후보자
        elif raw != tok:
            strength = 1       # 홍지선,  — 쉼표나 따옴표를 물고 있다
        else:
            continue
        if strength > best_strength:
            best, best_strength = tok, strength
        if best_strength == 2:
            break
    return best


def same_issue(a: dict, b: dict, thr: float) -> bool:
    """두 기사가 같은 이슈인가.

    글자 겹침만 보면 '서울 아파트값 84주째 올랐다' 와
    '쉴 새 없이 오르는 서울 집값…84주 연속 상승' 이 남남이 된다. 같은 숫자가
    제목에 박혀 있으면 문장이 꽤 달라도 같은 사건으로 친다.
    """
    s = sim(a["bg"], b["bg"])
    if s >= thr:
        return True
    return bool(a["num"] & b["num"]) and s >= thr / 2


def collect(cfg: dict, t0: datetime, t1: datetime, max_workers: int = 6) -> list[dict]:
    w = cfg.get("weekly") or {}
    outlets = w.get("outlets") or {}
    exclude = w.get("exclude") or []
    queries = w.get("queries") or []
    days = int(w.get("window_days", 14))
    allow_unlisted = bool(w.get("allow_unlisted", False))
    stop = name_stop(cfg)

    raw: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for got in pool.map(lambda q: _fetch(q, days), queries):
            raw.extend(got)

    items, seen = [], set()
    for it in raw:
        if not (t0 <= it["dt"] <= t1):
            continue
        outlet = outlet_of(it["src_url"], outlets)
        if not outlet and allow_unlisted:
            outlet = it["src_name"] or None
        if not outlet:
            continue

        title = _clean_title(it["title"], it["src_name"])
        low = title.lower()
        if not title or any(_contains(low, x) for x in exclude):
            continue
        key = (outlet, title)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "title": title,
            "link": it["link"],
            "outlet": outlet,
            "dt": it["dt"],
            "bg": bigrams(title),
            "num": numbers(title),
            "subj": person_of(title, stop) or subject(title),
        })

    log.info("주간 검색 %d개 → 기사 %d건, 기간·매체·제외 통과 %d건",
             len(queries), len(raw), len(items))
    return items


# ---------------------------------------------------------------- 선별

def rank(items: list[dict], cfg: dict) -> list[dict]:
    """카테고리 배점 + 몇 개 매체가 같은 이슈를 썼는지(빈도)로 줄 세운다.

    빈도를 세는 이유: 한 주의 '주요' 뉴스는 매체가 몰려 쓴 뉴스다.
    카테고리에 하나도 안 걸리는 기사는 여기서 통째로 빠진다.
    """
    w = cfg.get("weekly") or {}
    cats = w.get("categories") or []
    priority = set(w.get("priority") or [])
    thr = float(w.get("same_issue", 0.3))

    for it in items:
        low = it["title"].lower()
        it["cat"], weight = None, 0.0
        for c in cats:
            if any(_contains(low, x) for x in c.get("words") or []):
                it["cat"], weight = c["name"], float(c.get("weight", 1))
                break

        others = {o["outlet"] for o in items
                  if o is not it and same_issue(it, o, thr)}
        it["freq"] = len(others) + 1
        it["score"] = (weight
                       + 1.5 * min(len(others), 5)
                       + (0.5 if it["outlet"] in priority else 0))

    return sorted([i for i in items if i["cat"]], key=lambda x: -x["score"])


def pick(ranked: list[dict], cfg: dict) -> list[dict]:
    """카테고리마다 대표 1건을 먼저 확보한 뒤, 남은 자리를 점수순으로 채운다.

    점수순으로만 고르면 매체가 몰리는 가격동향·정책이 자리를 다 가져가고
    전월세·재개발이 통째로 빠진다. config 에 적힌 카테고리 순서가 우선순위다.
    """
    w = cfg.get("weekly") or {}
    n = int(w.get("count", 8))
    per_outlet = int(w.get("per_outlet", 2))
    thr = float(w.get("same_issue", 0.3))
    cats = w.get("categories") or []
    caps = {c["name"]: int(c.get("cap", 3)) for c in cats}
    subj_cap = int(w.get("per_subject", 1))
    generic = set(w.get("generic_subjects") or [])

    out: list[dict] = []
    used_outlet: dict[str, int] = {}
    used_subj: dict[str, int] = {}
    taken: set[int] = set()

    def ok(it: dict) -> bool:
        if id(it) in taken:
            return False
        if used_outlet.get(it["outlet"], 0) >= per_outlet:
            return False
        subj = it["subj"]
        if subj and subj not in generic and used_subj.get(subj, 0) >= subj_cap:
            return False
        if sum(o["cat"] == it["cat"] for o in out) >= caps.get(it["cat"], 3):
            return False
        return not any(same_issue(it, o, thr) for o in out)  # 같은 이슈는 대표 1건

    def add(it: dict) -> None:
        out.append(it)
        taken.add(id(it))
        used_outlet[it["outlet"]] = used_outlet.get(it["outlet"], 0) + 1
        if it["subj"]:
            used_subj[it["subj"]] = used_subj.get(it["subj"], 0) + 1

    for c in cats:                       # 1단계: 카테고리별 대표 1건
        if len(out) >= n:
            break
        for it in ranked:
            if it["cat"] == c["name"] and ok(it):
                add(it)
                break

    for it in ranked:                    # 2단계: 남은 자리는 점수순
        if len(out) >= n:
            break
        if ok(it):
            add(it)

    return sorted(out, key=lambda x: -x["score"])


def alternates(ranked: list[dict], picks: list[dict], cfg: dict) -> list[dict]:
    """고른 8건과 겹치지 않는 예비 후보. 4건을 직접 바꿔 끼울 때 본다."""
    w = cfg.get("weekly") or {}
    limit = int(w.get("alternates", 12))
    per_cat = int(w.get("alt_per_cat", 3))
    thr = float(w.get("same_issue", 0.3))

    out: list[dict] = []
    by_cat: dict[str, int] = {}
    for it in ranked:
        if len(out) >= limit:
            break
        if by_cat.get(it["cat"], 0) >= per_cat:   # 가격동향만 열 줄 나오는 것을 막는다
            continue
        if any(same_issue(it, o, thr) for o in picks + out):
            continue
        out.append(it)
        by_cat[it["cat"]] = by_cat.get(it["cat"], 0) + 1
    return out


# ---------------------------------------------------------------- 출력

def _esc(s: str) -> str:
    return html.escape(str(s or "").strip(), quote=False)


def render_paste(picks: list[dict], resolve_links: bool = True) -> str:
    """리포트에 그대로 붙이는 본문. 기사당 두 줄, 사이에 빈 줄 없음.

    이 메시지에는 다른 글자를 섞지 않는다 — 텔레그램에서 메시지 하나를
    복사하면 그대로 '지난주 주요 News Flow' 칸에 들어가야 한다.
    """
    links = [it["link"] for it in picks]
    if resolve_links:
        # 구글 뉴스 경유 주소는 리포트에 못 쓴다. 원문 주소로 펴는 데 건당 2회
        # 요청이 필요해서 8건을 동시에 돌린다.
        with ThreadPoolExecutor(max_workers=4) as pool:
            links = list(pool.map(extract.resolve, links))
    links = [strip_amp(u) for u in links]

    lines = []
    for it, link in zip(picks, links):
        d = it["dt"]
        lines.append(f"-{it['title']} ({d:%y}/{d.month}/{d.day},{it['outlet']})")
        lines.append("URL : " + link)
    return NEWLINE.join(lines)


def render_memo(cfg: dict, t0: datetime, t1: datetime, found: int,
                picks: list[dict], alts: list[dict]) -> str:
    out = [
        "<b>🏠 부동산 주간 News Flow</b>",
        f"<i>{_fmt_day(t0)} 00:00 ~ {_fmt_day(t1)} {t1:%H:%M} · "
        f"후보 {found}건 중 {len(picks)}건</i>",
        "",
        f"<b>━━ 고른 {len(picks)}건</b>",
    ]
    for i, it in enumerate(picks, 1):
        more = f" · {it['freq']}개 매체" if it["freq"] > 1 else ""
        out.append(f"{i}. [{_esc(it['cat'])}] {_esc(it['outlet'])}{more}")

    if alts:
        out += ["", "<b>━━ 바꿔 쓸 후보</b>", ""]
        for it in alts:
            more = f" <i>({it['freq']})</i>" if it["freq"] > 1 else ""
            out.append(f"[{_esc(it['cat'])}] " + '<a href="' + _esc(it["link"]) + '">'
                       + _esc(it["title"]) + "</a>" + more)
            out.append("")

    out.append("<i>아래 메시지를 그대로 복사해 '지난주 주요 News Flow' 칸에 붙이세요. "
               "(N) 은 같은 이슈를 쓴 매체 수</i>")
    return NEWLINE.join(out)


# ---------------------------------------------------------------- 실행

def run(bot, store, cfg: dict, force: bool = False) -> int:
    """마감 시각이 지났고 이번 주 것을 아직 안 보냈으면 보낸다.

    5분마다 도는 일반 실행에 얹혀 있다. 워크플로를 따로 두지 않는 이유는
    GitHub 예약이 몇 시간씩 밀리기 때문 — 이미 제 시각에 깨워주는 앱스 스크립트
    트리거에 붙이는 편이 금요일 08시를 지킨다.
    """
    w = cfg.get("weekly") or {}
    if not w.get("enabled", True) and not force:
        return 0

    due = deadline(cfg)
    last = float(store.data.get("last_weekly") or 0)
    if not force:
        if last <= 0:
            # 처음 켠 주는 건너뛴다. 아니면 수요일에 설치했을 때 지난주 리포트가
            # 느닷없이 날아온다. 지금 당장 보고 싶으면 --weekly-now 로 돌리세요.
            store.data["last_weekly"] = due.timestamp()
            log.info("주간 리포트 기준점을 %s 로 잡았습니다 — 다음 마감부터 보냅니다",
                     due.strftime("%m/%d %H:%M"))
            return 0
        if last >= due.timestamp():
            return 0

    t0, t1 = window(cfg)
    log.info("주간 리포트 작성 — 대상 기간 %s ~ %s", _fmt_day(t0), _fmt_day(t1))

    items = collect(cfg, t0, t1)
    if not items:
        # 네트워크가 통째로 죽은 경우다. 도장을 찍지 않고 다음 실행에 다시 시도한다.
        log.warning("주간 후보를 한 건도 못 모았습니다 — 다음 실행에서 재시도")
        return 0

    ranked = rank(items, cfg)
    picks = pick(ranked, cfg)
    store.data["last_weekly"] = due.timestamp()

    if not picks:
        bot.send(f"🏠 부동산 주간 News Flow — {_fmt_day(t1)} 기준 후보 "
                 f"{len(items)}건 중 기준에 맞는 기사를 못 찾았습니다.", channel="clip")
        return 0

    bot.send(render_memo(cfg, t0, t1, len(ranked), picks,
                         alternates(ranked, picks, cfg)), channel="clip")
    bot.send(render_paste(picks, bool(w.get("resolve_links", True))),
             channel="clip", plain=True)
    log.info("주간 리포트 발송 %d건", len(picks))
    return len(picks)
