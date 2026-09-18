"""기사 본문 추출."""

from __future__ import annotations

import html as htmllib
import json
import logging
import re
import urllib.parse

import requests
import trafilatura

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

URL_RE = re.compile(r"https?://[^\s<>\"'()]+")


def find_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,)]") if m else None


_GN_BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
_GN_SG = re.compile(r'data-n-a-sg="([^"]+)"')
_GN_TS = re.compile(r'data-n-a-ts="(\d+)"')
_GN_RES = re.compile(r'\[\\"garturlres\\",\\"(.*?)\\"')


def _resolve_google(url: str, timeout: int) -> str | None:
    """구글뉴스의 불투명 링크를 원본 기사 주소로 바꾼다.

    구글이 2024년부터 원본 URL을 인코딩하지 않고 내부 ID로 바꿔버려서,
    기사 페이지에서 서명(sg)과 타임스탬프(ts)를 꺼내 비공개 엔드포인트에 물어봐야 한다.
    구글이 언제든 바꿀 수 있는 방식이라 실패하면 조용히 원래 링크를 쓴다.
    """
    try:
        article_id = url.split("/articles/")[1].split("?")[0]
    except IndexError:
        return None

    try:
        with requests.Session() as s:
            s.headers.update(HEADERS)
            html = s.get(url, timeout=timeout).text
            sg, ts = _GN_SG.search(html), _GN_TS.search(html)
            if not (sg and ts):
                return None

            inner = json.dumps(
                [
                    "garturlreq",
                    [
                        ["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                         None, None, None, None, None, 0, 1],
                        "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0,
                    ],
                    article_id,
                    int(ts.group(1)),
                    sg.group(1),
                ]
            )
            r = s.post(
                _GN_BATCH,
                data={"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])},
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
                timeout=timeout,
            )
        m = _GN_RES.search(r.text)
        if not m:
            return None
        # 두 겹으로 JSON 인코딩되어 있어 (\\u003d 등) 한 번 더 디코드해야 한다
        resolved = json.loads(f'"{m.group(1)}"')
        resolved = json.loads(f'"{resolved}"') if "\\u" in resolved else resolved
        if resolved.startswith("http") and "news.google.com" not in resolved:
            return resolved
    except (requests.RequestException, ValueError, KeyError) as e:
        log.info("구글뉴스 링크 해석 실패: %s", e)
    return None


def resolve(url: str, timeout: int = 20) -> str:
    """리다이렉트 링크를 원본 기사 주소로 편다. 실패하면 원래 링크 그대로."""
    if "news.google.com" not in urllib.parse.urlparse(url).netloc:
        return url
    return _resolve_google(url, timeout) or url


_TITLE_META = re.compile(
    r"""<meta[^>]+(?:property|name)=["'](?:og:title|twitter:title)["'][^>]+"""
    r"""content=["']([^"']+)""",
    re.I,
)
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
# '제목 | Pluang', '제목 - 연합뉴스' 처럼 뒤에 붙는 사이트 이름
_SITE_TAIL = re.compile(r"\s*[|\-–—·]\s*[^|\-–—·]{1,30}\s*$")

# 이보다 짧으면 본문이 아니라 '관련 기사' 카드나 미리보기 블록일 가능성이 크다.
# 실측: pluang.com 에서 328자짜리 관련뉴스 카드가 본문으로 통과해 엉뚱한 기사를
# 요약했다. 짧은 단신이 여기 걸려도 손해는 '본문 부족' 경고뿐이다.
MIN_BODY = 600


def page_title(html: str) -> str:
    """기사 페이지의 제목. og:title 을 먼저 보고 없으면 <title>."""
    m = _TITLE_META.search(html) or _TITLE_TAG.search(html)
    if not m:
        return ""
    t = htmllib.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    return _SITE_TAIL.sub("", t).strip() or t


def article_text(url: str, max_chars: int = 12000,
                 timeout: int = 20) -> tuple[str, bool, str]:
    """(본문, 성공여부, 페이지 제목). 실패하면 본문은 빈 문자열.

    제목을 함께 돌려주는 이유: 내가 링크만 보낼 때는 기사 제목을 알 수 없어서,
    추출기가 엉뚱한 글을 물어와도 대조할 기준이 없다. 제목이 있으면 모델이
    "제목은 주가 급등인데 본문은 CEO 매도" 라는 어긋남을 알아챈다.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        if "html" not in r.headers.get("content-type", "").lower():
            return "", False, ""
        html = r.text
    except requests.RequestException as e:
        log.info("본문 요청 실패 %s: %s", url, e)
        return "", False, ""

    title = page_title(html)
    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
        no_fallback=False,
    )
    if not text or len(text) < MIN_BODY:
        if text:
            log.info("본문이 %d자뿐 — 관련뉴스 카드일 수 있음: %s", len(text), url)
        return (text or "")[:max_chars], False, title
    return text[:max_chars], True, title


def clean_html(s: str) -> str:
    """RSS summary 에 섞인 태그 제거 (본문 추출 실패 시 대체재로 씀)."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&[a-z]+;|&#\d+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()
