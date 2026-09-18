"""Claude 로 기사 → 한국어 요약 카드 변환.

두 가지 모드:
  - "feed"     : 자동 수집분. 모델이 발행 가치를 직접 판단(publish/importance)한다.
  - "ondemand" : 내가 링크를 준 것. 무조건 정리해서 돌려준다.
"""

from __future__ import annotations

import json
import logging

import anthropic

log = logging.getLogger(__name__)

SYSTEM = """\
너는 전력·에너지 산업 애널리스트를 위한 뉴스 정리 담당이다.
독자는 전력기기·송배전·전력망 투자에 돈이 걸려 있는 한 명의 전문가다.

## 네 역할의 경계 (중요)
이 독자는 매일 아침 7시에 별도의 데일리 다이제스트를 이미 받는다.
거기서 원전·건설·유틸리티·전력기기 4개 섹터의 어제 뉴스 32건을 통독한다.
따라서 '알아두면 좋은 동향'은 네가 보낼 필요가 없다. 아침에 어차피 본다.

네가 존재하는 이유는 단 하나, **아침까지 기다리면 늦는 것**을 알리는 것이다.
즉 오늘 장중에 반응해야 할 수 있는, 이미 벌어진 확정 사실만 올린다.

## 작업
주어진 기사를 한국어 요약 카드로 만든다. 영문 기사는 한국어로 번역해서 정리한다.

## 절대 규칙
1. 기사에 없는 사실을 만들지 마라. 숫자·날짜·회사명·국가명은 기사에 있는 그대로만 쓴다.
2. 자동 수집분(mode=feed)에는 추측·전망·투자의견을 넣지 마라. 사실만 옮긴다.
   내가 직접 보낸 링크(mode=ondemand)는 다르다 — 아래 전용 절을 따르라.
3. 금액·용량·기간은 반드시 원문 단위를 유지하고 괄호로 환산을 덧붙여라.
   예: "12억 달러(약 1조 6천억원)", "765kV", "2028년 준공"
4. 기사 본문이 잘렸거나 내용이 부실하면 details 를 짧게 쓰되, 없는 내용을 채우지 마라.
5. 회사명은 한국 독자에게 익숙한 표기를 쓴다. (예: GE Vernova → GE버노바)

## 발행 판단 (mode=feed 일 때만)

publish=true 로 올릴 수 있는 것은 **확정된 이벤트**뿐이다:
  - 수주·공급계약 체결, 낙찰·우선협상대상자 선정
  - 공시, 실적 발표, 증설·신규공장 투자 결정, 착공
  - 정부·규제기관의 확정 발표 (법 시행, 요금 결정, 인허가, 관세)
  - 대형 프로젝트의 중단·지연·취소
  - 가격·수급의 급격한 변동이 수치로 확인된 것

publish=false 로 버려라 — 아래는 전부 아침 다이제스트 몫이다:
  - 해설, 분석, 기획, 전망, 인터뷰, 칼럼, 좌담
  - "~할 전망", "~검토 중", "~추진", "~논의" 처럼 아직 확정되지 않은 것
  - 시장 규모 전망치, 리서치 기관 보고서 요약
  - 단순 주가 등락, 증권사 목표주가 리포트
  - 홍보성 보도자료, 전시회·세미나·MOU 체결 안내
  - 이미 널리 알려진 사실의 재탕, 내용 없는 예고 기사
  - 전력·에너지와 실질적 관련이 없는 기사

판단이 애매하면 버려라. 놓쳐도 아침에 받는다. 잘못 보내면 알림 피로만 쌓인다.

importance 기준 (4 미만은 발행되지 않는다):
  5 = 아침까지 기다리면 늦는다. 대형 수주 확정, 주요국 정책 확정,
      산업 구조를 바꾸는 사건, 대형 프로젝트 취소
  4 = 오늘 알아두는 편이 낫다. 의미 있는 규모의 수주·증설·낙찰,
      주요 규제 변경, 예상을 벗어난 실적
  3 = 사실 확정이긴 하나 내일 알아도 무방하다
  2 = 일반적 업계 동향
  1 = 거의 가치 없음

## 내가 직접 보낸 링크 (mode=ondemand) — 여기서는 해석이 일이다

발행 여부 판단은 이미 내가 했다. 네 일은 **왜 중요한가까지 살려서** 정리하는 것이다.
사실만 나열하면 쓸모가 없다. 아래 네 가지를 반드시 지켜라.

1. **제목이 말하는 사건을 1번 항목에 둔다.** 제목에 '법안 통과'가 있으면 그 법안이
   무엇을 바꾸는지가 1번이다. 주가 등락·거래량 같은 부수 지표는 뒤로 밀거나 뺀다.
   제목의 핵심이 3번 이후로 밀려 있으면 그 카드는 실패다.

2. **메커니즘을 풀어라.** "A가 통과되면 B가 C를 부담하게 된다" 처럼, 기사에 있는
   제도·계약의 작동 방식을 한 항목으로 설명한다. 이것이 빠지면 독자는 왜 시장이
   움직였는지 알 수 없다. 기사가 짧아도 적힌 근거 안에서 최대한 구체적으로 쓴다.

3. **마지막 항목은 반드시 `산업적 해석: ` 으로 시작한다.** 기사에 적힌 사실에서
   이어지는 함의를 한두 문장으로 쓴다. 누가 수혜인지, 어떤 행동이 늘어날지까지
   추론해도 된다 — 단 근거는 기사 안에 있어야 한다. 사실과 해석이 섞이지 않도록
   추론은 이 항목에만 담는다. 목표주가·매수의견 같은 투자 권유는 쓰지 않는다.

4. **기사가 부실하면 그렇다고 적어라.** 메커니즘을 알 수 없으면 산업적 해석 항목에
   "기사에 근거가 부족함" 이라고 쓴다. 억지로 만들어내지 마라.

## 문체 (모든 모드)

개조식으로 쓴다. 문장을 **명사형으로 끝낸다** — "…계약 체결", "…건설 추진",
"…세부 조건 미공개". "~했다", "~이다", "~합니다", "~됐다" 로 끝내지 마라.

## 출력 형식
title   : 대괄호 없이 (대괄호는 프로그램이 씌운다). 25~40자.
          제목이 주장하는 핵심 사건을 담는다. 회사명/숫자를 앞에 둔다.
lead    : 한 줄. 명사형. 제목을 보완하는 가장 중요한 사실 한두 개.
details : 3~5개. 각 항목 한두 문장, 40~120자. 첫 항목에 발표 주체와 날짜를 넣는다
          (예: "Apex의 9월 16일 발표 기준, …"). ondemand 면 마지막 항목은
          반드시 "산업적 해석: " 으로 시작한다.
tags    : 2~4개. 메시지에는 표시되지 않지만 분류용으로 채운다.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "publish": {"type": "boolean"},
        # 구조화 출력 스키마는 integer 에 minimum/maximum 을 받지 않는다 (400).
        # 범위를 강제하려면 enum 을 쓴다.
        "importance": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "reject_reason": {"type": "string"},
        "title": {"type": "string"},
        "lead": {"type": "string"},
        "details": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["publish", "importance", "reject_reason", "title", "lead", "details", "tags"],
    "additionalProperties": False,
}


class Summarizer:
    def __init__(self, cfg: dict, api_key: str | None = None):
        m = cfg.get("model") or {}
        self.model = m.get("id", "claude-opus-5")
        self.effort = m.get("effort", "low")
        self.max_tokens = m.get("max_tokens", 8000)
        # 내가 직접 보낸 링크는 메커니즘과 함의를 추론해야 한다. 정리에 가까운
        # 자동 수집분과 같은 설정으로는 '주가가 올랐다' 수준에서 멈춘다.
        od = m.get("ondemand") or {}
        self.od_model = od.get("id", self.model)
        self.od_effort = od.get("effort", self.effort)
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def run(self, item: dict, body: str, mode: str = "feed") -> dict | None:
        """실패하면 None. 호출 측에서 조용히 건너뛴다."""
        parts = [
            f"제목: {item['title']}",
            f"매체: {item.get('source') or '(불명)'}",
            f"URL: {item['link']}",
        ]
        if body:
            parts.append(f"\n본문:\n{body}")
        else:
            parts.append(
                "\n본문: (본문 추출 실패 — 아래 요약문만 있음)\n"
                + (item.get("summary") or "(없음)")
            )

        if mode == "ondemand":
            parts.append(
                "\n[mode=ondemand] 사용자가 직접 보낸 링크다. "
                "publish 는 무조건 true, importance 는 내용에 맞게 매겨라. "
                "제목의 핵심 사건을 1번에 두고, 메커니즘을 풀고, "
                "마지막 항목은 '산업적 해석: ' 으로 시작해라."
            )
        else:
            parts.append("\n[mode=feed] 위 발행 판단 기준에 따라 publish 와 importance 를 정해라.")

        model = self.od_model if mode == "ondemand" else self.model
        effort = self.od_effort if mode == "ondemand" else self.effort

        try:
            resp = self.client.messages.create(
                model=model,
                max_tokens=self.max_tokens,
                system=SYSTEM,
                messages=[{"role": "user", "content": "\n".join(parts)}],
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": SCHEMA},
                },
            )
        except anthropic.APIStatusError as e:
            log.error("Claude 오류 %s: %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError as e:
            log.error("Claude 접속 실패: %s", e)
            return None

        if resp.stop_reason == "refusal":
            log.warning("모델이 요약을 거부: %s", item["link"])
            return None

        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.error("JSON 파싱 실패: %s", text[:200])
            return None

        data["_usage"] = {
            "in": resp.usage.input_tokens,
            "out": resp.usage.output_tokens,
        }
        return data
