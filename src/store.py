"""상태 저장소.

GitHub Actions 는 실행이 끝나면 디스크가 사라지므로, 이 JSON 파일을
저장소에 다시 커밋해서 다음 실행이 이어받게 한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# 오래된 기록은 버린다 (파일이 무한히 커지지 않도록)
SEEN_TTL_DAYS = 21
MAX_TITLE_FINGERPRINTS = 400

# 같은 사건을 다른 매체가 쓴 기사 판정 기준.
# 한국어 제목은 조사가 붙어서 단어 단위 비교가 안 통하므로 글자 3-gram 을 쓴다.
NGRAM_N = 3
DUP_STRONG = 0.45     # 숫자 단서가 없을 때, 글자 겹침만으로 중복 판정하는 선
DUP_WEAK = 0.18       # 핵심 숫자가 같을 때 요구하는 최소 겹침
DUP_IDENTICAL = 0.85  # 숫자가 서로 다른데도 중복으로 볼 만큼 제목이 똑같은 경우
NUM_SIGFIGS = 2       # 3865억 과 3866억 을 같은 숫자로 보기 위한 반올림 자릿수


def now_kst() -> datetime:
    return datetime.now(KST)


def url_key(url: str) -> str:
    """추적 파라미터를 떼어낸 정규화 URL의 해시."""
    u = url.split("#")[0]
    u = re.sub(r"[?&](utm_[^=]+|fbclid|gclid|ref|src|CMP|mc_cid|mc_eid)=[^&]*", "", u)
    u = u.rstrip("?&/").lower()
    return hashlib.sha1(u.encode("utf-8")).hexdigest()[:16]


_LEAD_BRACKET = re.compile(r"^\s*[\[\(【][^\]\)】]{0,20}[\]\)】]\s*")
_TRAIL_OUTLET = re.compile(r"\s*[-–—|·]\s*[^-–—|·]{1,18}\s*$")
_NUM_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(억원|억|조원|조|만원|만|천억|백만|kV|㎸|MW|GW|MVA|%|달러|원)?",
    re.IGNORECASE,
)
# 매체마다 '3865억' / '3865억원' 으로 갈리므로 같은 단위로 묶는다
_UNIT_CANON = {"억원": "억", "조원": "조", "만원": "만", "㎸": "kv", "kv": "kv"}


def normalize_title(title: str) -> str:
    """'[속보] ... - 머니투데이 - 머니투데이' 같은 껍데기를 벗긴다."""
    t = title.strip()
    for _ in range(2):
        t = _LEAD_BRACKET.sub("", t)
        t = _TRAIL_OUTLET.sub("", t)
    return t.strip()


def char_ngrams(title: str, n: int = NGRAM_N) -> set[str]:
    s = re.sub(r"[^0-9a-z가-힣]", "", normalize_title(title).lower())
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def number_signature(title: str) -> set[str]:
    """제목 속 수치를 유효숫자 2자리로 뭉개 '3865억원'과 '3866억'을 같게 만든다.

    매체마다 단위 표기가 달라서(억 / 억원) 단위도 함께 정규화한다.
    연도로 보이는 네 자리 숫자는 서로 다른 기사끼리 우연히 겹치므로 뺀다.
    """
    sig = set()
    for m in _NUM_RE.finditer(normalize_title(title)):
        raw = m.group(1).replace(",", "")
        if len(raw.replace(".", "")) < 2:  # 한 자리 숫자는 변별력이 없다
            continue
        try:
            v = float(raw)
        except ValueError:
            continue
        if v == 0:
            continue

        unit = _UNIT_CANON.get((m.group(2) or "").lower(), (m.group(2) or "").lower())
        if not unit and 1900 <= v <= 2100 and v.is_integer():
            continue  # 연도

        exp = math.floor(math.log10(abs(v)))
        step = 10 ** (exp - (NUM_SIGFIGS - 1))
        sig.add(f"{round(v / step) * step:g}{unit}")
    return sig


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_same_story(title_a: str, title_b: str) -> bool:
    j = jaccard(char_ngrams(title_a), char_ngrams(title_b))
    na, nb = number_signature(title_a), number_signature(title_b)

    if na and nb:
        if na & nb:
            # 같은 금액/규모가 들어있으면 문장이 꽤 달라도 같은 사건이다
            return j >= DUP_WEAK
        # 양쪽 다 숫자가 있는데 하나도 안 겹치면 다른 사건으로 본다.
        # '효성중공업, 1665억원 규모 공급계약 체결' 과 '…2200억원 규모…' 는
        # 제목 틀이 같아서 글자만 보면 거의 똑같지만 실제로는 별개 공시다.
        return j >= DUP_IDENTICAL

    return j >= DUP_STRONG


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = {
            "seen": {},            # url_key -> unix ts
            "titles": [],          # [[ts, [tokens...]], ...] 최근 제목 지문
            "queue": [],           # 조용한 시간대에 밀린 발행물
            "alerted": {},         # url_key -> ts  (이미 울린 알람)
            "alert_titles": [],    # 알람용 제목 지문 (같은 사건 재알람 방지)
            "clipped": {},         # url_key -> ts  (이미 클리핑에 담은 기사)
            "clip_titles": [],     # 클리핑용 제목 지문
            "clip_buffer": [],     # 다음 클리핑에 나갈 기사들
            "last_clip": 0,        # 마지막 클리핑 발송 시각 (unix)
            "alerted_today": 0,
            "alert_cooldown": {},  # "회사|규칙" -> 마지막 알람 시각
            "day": "",             # 'YYYY-MM-DD' (KST)
            "posted_today": 0,
            "companies_today": {}, # 회사명 -> 오늘 발행 수
            "tg_offset": 0,        # 텔레그램 getUpdates 오프셋
            "paused": False,
            "stats": {"total_posted": 0, "last_run": ""},
        }
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self.data.update(loaded)
            except (json.JSONDecodeError, OSError):
                pass  # 손상된 상태 파일은 버리고 새로 시작
        self._roll_day()

    # ---------- 날짜/한도 ----------

    def _roll_day(self) -> None:
        today = now_kst().strftime("%Y-%m-%d")
        if self.data.get("day") != today:
            self.data["day"] = today
            self.data["posted_today"] = 0
            self.data["companies_today"] = {}
            self.data["alerted_today"] = 0

    def remaining_today(self, cap: int) -> int:
        self._roll_day()
        return max(0, cap - int(self.data["posted_today"]))

    def count_post(self, companies: list[str] | None = None) -> None:
        self._roll_day()
        self.data["posted_today"] += 1
        self.data["stats"]["total_posted"] = self.data["stats"].get("total_posted", 0) + 1
        for name in companies or []:
            self.data["companies_today"][name] = self.data["companies_today"].get(name, 0) + 1

    def company_quota_left(self, companies: list[str], cap: int) -> bool:
        """오늘 이 회사 건을 더 보내도 되는가.

        큰 수주 하나가 여러 공시로 쪼개져 나오거나(2200억 + 1665억 = 3865억),
        매체마다 다른 각도로 쓰면 숫자 지문만으로는 같은 사건인 줄 모른다.
        회사 단위로 한 번 더 조이면 그런 것까지 걸린다.
        """
        self._roll_day()
        if cap <= 0:
            return True  # 0 이하면 제한 없음
        return all(self.data["companies_today"].get(n, 0) < cap for n in companies)

    # ---------- 중복 판정 ----------

    def is_new(self, url: str, title: str) -> bool:
        """이미 다뤘거나, 같은 사건을 다른 매체가 쓴 기사면 False."""
        if url_key(url) in self.data["seen"]:
            return False
        if not title:
            return True
        for _ts, prev in self.data["titles"]:
            if isinstance(prev, str) and is_same_story(title, prev):
                return False
        return True

    def mark(self, url: str, title: str) -> None:
        self.data["seen"][url_key(url)] = int(time.time())
        if title:
            self.data["titles"].append([int(time.time()), normalize_title(title)])

    # ---------- 알람 / 클리핑 ----------

    def _is_dup(self, url: str, title: str, seen_key: str, titles_key: str) -> bool:
        if url_key(url) in self.data[seen_key]:
            return True
        if not title:
            return False
        return any(isinstance(p, str) and is_same_story(title, p)
                   for _ts, p in self.data[titles_key])

    def _record(self, url: str, title: str, seen_key: str, titles_key: str) -> None:
        self.data[seen_key][url_key(url)] = int(time.time())
        if title:
            self.data[titles_key].append([int(time.time()), normalize_title(title)])

    def already_alerted(self, url: str, title: str) -> bool:
        return self._is_dup(url, title, "alerted", "alert_titles")

    def mark_alerted(self, url: str, title: str) -> None:
        self._record(url, title, "alerted", "alert_titles")
        self.data["alerted_today"] = self.data.get("alerted_today", 0) + 1

    def already_clipped(self, url: str, title: str) -> bool:
        # 클리핑은 같은 사건의 다른 기사도 목록에 남길 값어치가 있으므로
        # URL 만 본다. 제목 지문까지 보면 매체별 시각 차이가 사라진다.
        return url_key(url) in self.data["clipped"]

    def mark_clipped(self, url: str, title: str) -> None:
        self.data["clipped"][url_key(url)] = int(time.time())

    def in_cooldown(self, company: str, rule: str, hours: float) -> bool:
        """같은 회사의 같은 유형 사건을 연달아 알리지 않는다.

        큰 수주 하나를 매체 열 곳이 제각각 다른 제목으로 쓰면 제목 지문만으로는
        같은 사건인 줄 모른다. (회사, 규칙) 쌍에 쿨다운을 두면 그게 걸린다.
        다른 유형(예: 통상·정책)은 막지 않으므로 성격이 다른 사건은 그대로 울린다.
        """
        if hours <= 0 or not company:
            return False
        last = self.data["alert_cooldown"].get(company + "|" + rule, 0)
        return time.time() - float(last) < hours * 3600

    def touch_cooldown(self, company: str, rule: str) -> None:
        if company:
            self.data["alert_cooldown"][company + "|" + rule] = int(time.time())

    def alerts_left_today(self, cap: int) -> int:
        self._roll_day()
        return max(0, cap - int(self.data.get("alerted_today", 0)))

    def clip_due(self, interval_hours: float) -> bool:
        if not self.data["clip_buffer"]:
            return False
        return time.time() - float(self.data.get("last_clip") or 0) >= interval_hours * 3600

    def flush_clip(self) -> list[dict]:
        rows = self.data["clip_buffer"]
        self.data["clip_buffer"] = []
        self.data["last_clip"] = int(time.time())
        return rows

    # ---------- 조용한 시간 큐 ----------

    def enqueue(self, payload: dict) -> None:
        self.data["queue"].append(payload)

    def drain_queue(self) -> list[dict]:
        q = self.data["queue"]
        self.data["queue"] = []
        return q

    # ---------- 텔레그램 오프셋 ----------

    @property
    def tg_offset(self) -> int:
        return int(self.data.get("tg_offset", 0))

    @tg_offset.setter
    def tg_offset(self, v: int) -> None:
        self.data["tg_offset"] = int(v)

    @property
    def paused(self) -> bool:
        return bool(self.data.get("paused", False))

    @paused.setter
    def paused(self, v: bool) -> None:
        self.data["paused"] = bool(v)

    # ---------- 저장 ----------

    def save(self) -> None:
        cutoff = int(time.time()) - SEEN_TTL_DAYS * 86400
        for key in ("seen", "alerted", "clipped"):
            self.data[key] = {k: v for k, v in self.data[key].items() if v >= cutoff}
        self.data["alert_cooldown"] = {
            k: v for k, v in self.data["alert_cooldown"].items() if v >= cutoff
        }
        for key in ("titles", "alert_titles", "clip_titles"):
            self.data[key] = self.data[key][-MAX_TITLE_FINGERPRINTS:]
        self.data["stats"]["last_run"] = now_kst().strftime("%Y-%m-%d %H:%M KST")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)
