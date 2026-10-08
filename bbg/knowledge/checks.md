# 검증 규칙 (생성기가 자동 체크할 것)

## 1. 실적 vs 컨센 기준 일치  ★최우선
- BEST_ 컨센은 보통 조정(Non-GAAP) 기준, IS_* 실적은 GAAP 기준일 가능성
- 같은 표에서 실적→컨센 이어질 때 성장률·서프라이즈 왜곡
- TODO: 블벅 수식 자료로 조정 실적 필드 / GAAP 컨센 필드 확인 후 fields.yaml basis 확정
- 터미널 확인: NVDA 직전 분기 IS_DIL_EPS_CONT_OPS vs 발표 GAAP EPS, BEST_EPS vs Non-GAAP EPS

## 2. 기간 연속성
- 실적 마지막 분기(하드코딩) 다음이 1FQ여야 함. 시간 지나면 공백/중복 발생
- 라벨은 마지막 실적 분기 기준 자동 생성 (기존 템플릿 라벨 오류: 2Q26E,3Q26E,4Q26E,3Q27E… 건너뜀)
- 결산월 다른 기업(NVDA 1월 결산: FQ1 2026 = 2025.2~4월) — 회계연도/달력연도 표기 결정 필요

## 3. 통화
- EQY_FUND_CRNCY 하나로 통일 (기존: 분기 EQY_FUND_CRNCY=USD, 연간 Currency=USD 혼용)
- ADR/해외상장(TSM US 등): 주가 통화 ≠ 재무 통화 → PER/PBR 왜곡 경고

## 4. 단위
- SCALING_FORMAT=MLN → 현지통화 백만. 표 헤더 단위 자동 표기
