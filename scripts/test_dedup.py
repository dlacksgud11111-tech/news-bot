"""중복 판정 회귀 테스트.

    python scripts/test_dedup.py

같은 사건을 여러 매체가 쓴 기사는 한 번만 나가야 하고,
제목 틀이 같지만 금액이 다른 별개 공시는 각각 나가야 한다.
config.yaml 의 임계값(store.py 상단 DUP_*)을 건드렸다면 이걸 돌려보세요.
실제 수집된 제목들로 만든 케이스입니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from store import char_ngrams, is_same_story, jaccard, number_signature  # noqa: E402

CASES: list[tuple[bool, str, str, str]] = [
    (True,
     "효성중공업, 美 빅테크에 초고압변압기 3865억원 수주 - 일간투데이",
     "[공시 Pick] 효성중공업, 美 초고압변압기 3866억 공급계약…주가는 약세 - 뉴스투데이",
     "같은 수주 건 (금액 반올림 오차, 대괄호 머리말)"),
    (True,
     "효성중공업, 美 빅테크에 초고압변압기 3865억원 수주 - 일간투데이",
     "조현준 승부수 또 통했다…효성重, 美 빅테크 3865억 초고압변압기 수주 - 뉴스1",
     "같은 수주 건 (제목 스타일이 완전히 다름)"),
    (True,
     "효성중공업, 美 빅테크에 초고압변압기 3865억원 수주 - 일간투데이",
     "효성중공업, 美 빅테크 2곳서 변압기 3865억원 수주 - 시사포커스",
     "같은 수주 건"),
    (False,
     "효성중공업, 1665억원 규모 공급계약 체결 - 데이터투자",
     "효성중공업, 2200억원 규모 공급계약 체결 - 데이터투자",
     "제목 틀은 같지만 금액이 다른 별개 공시"),
    (False,
     "효성중공업, 美 빅테크에 초고압변압기 3865억원 수주 - 일간투데이",
     "효성중공업, 1665억원 규모 공급계약 체결 - 데이터투자",
     "같은 회사의 별개 건"),
    (False,
     "LS에코에너지-가온전선, 손잡고 북미 시장 공략 가속화",
     "가온전선, 170억 규모 수상태양광 시장 첫 진입… ACF 케이블 입지 넓힌다",
     "같은 회사의 별개 기사"),
    (True,
     "GE Vernova wins $1.2bn Texas transmission contract",
     "GE Vernova awarded 1.2 billion dollar Texas 765kV transmission deal",
     "같은 해외 수주 (영문)"),
    (False,
     "HD현대일렉트릭, 3분기 영업이익 2000억 돌파",
     "HD현대일렉트릭, 미국 앨라배마 공장 증설에 5000만달러 투자",
     "실적 기사 vs 투자 기사"),
    (False,
     "한전, 2028년까지 송전망 투자 확대",
     "한전, 2030년까지 배전망 자동화 추진",
     "연도가 우연히 비슷한 별개 기사"),
]


def main() -> int:
    passed = 0
    for expected, a, b, why in CASES:
        got = is_same_story(a, b)
        j = jaccard(char_ngrams(a), char_ngrams(b))
        ok = got == expected
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  기대={str(expected):<5} 결과={str(got):<5} "
              f"겹침={j:.2f}  {why}")
        if not ok:
            print(f"        A 숫자={sorted(number_signature(a))}")
            print(f"        B 숫자={sorted(number_signature(b))}")

    print(f"\n{passed}/{len(CASES)} 통과")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
