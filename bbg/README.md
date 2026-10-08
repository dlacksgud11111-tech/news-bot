# bbg — 해외기업 블룸버그 수식 생성기

해외 리뷰·프리뷰 페이퍼에 들어가는 데이터를 블룸버그 Excel 수식(BDP/BDH/BDS)으로
한 번에 뽑아내기 위한 지식 베이스 + 수식 생성기.

## 흐름
1. 페이퍼(`papers/`) 분석 → 들어가면 좋은 항목 정리 (`knowledge/items.md`)
2. 항목 → 블룸버그 필드 매핑 (`knowledge/fields.yaml`)
3. 실적/컨센 기준 일치 등 검증 규칙 (`knowledge/checks.md`)
4. 생성기: 티커 + 기간 → RAW 시트 수식 일괄 생성 (예정)

## 공통 규칙
- 통화: 기업 현지 통화. 모든 수식이 통화 셀 하나를 참조, `EQY_FUND_CRNCY=<통화>`로 통일
- 단위: `SCALING_FORMAT=MLN` (현지통화 백만)
- 실적: `BDH(..., "Period=FQ|FY", "FILING_STATUS=MR", "FA_ADJUSTED=Adjusted", ...)`
- 컨센: `BDP(티커, BEST_필드, "BEST_FPERIOD_OVERRIDE=nFQ|nFY", 통화)`
