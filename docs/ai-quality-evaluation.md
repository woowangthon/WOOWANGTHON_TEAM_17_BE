# AI 파싱·충돌·일정 품질 검증

이 평가는 **합성 사용자 1~5**와 별도 충돌 변형으로 Gemini의 동작과 일정 개선을 확인한다. 실제 참여자 5명의 조사나 실제 여행 만족도 실험이 아니다. 사람이 제출한 평가가 없으면 만족도는 `null`/미측정으로 남긴다.

## 무엇을 구분하는가

| 구분 | 검증하는 것 | 검증하지 않는 것 |
| --- | --- | --- |
| 오프라인 회귀 검사 | 합성 응답·SQLite·고정 이동시간에서 코드와 오류 처리가 계약을 지키는지 | 실제 Gemini의 의미 해석 품질 |
| 실제 Gemini 호출 | 고정된 한국어 합성 입력의 조건 추출·누락·추측과 생성 실패 | 실제 사용자를 대표하는 정확도 |
| 고정 자료의 일정 비교 | 같은 AI 초안과 카탈로그/이동시간에서 순서·선택·날짜 배분 효과 | 실시간 교통·장소 영업·전체 비용 보장 |
| 사람 평가 | 사람이 직접 기록한 취향 반영·이동 부담·사용 의향 | AI 점수만으로 추정한 사용자 만족도 |

## 평가 절차

1. 입력과 기대 조건을 먼저 고정하고, 입력·카탈로그·이동시간 자료의 hash를 기록한다. 기본 5개와 충돌 변형 수를 따로 센다. 개발에 쓴 동일 예제로 측정한 결과는 개발 평가이며 독립적인 최종 평가가 아니다.
2. 명시적 live 실행에서만 Gemini를 호출한다. 같은 응답을 개선 전/후의 보수적 도보 근거 검사에 통과시켜 모델 변동과 후처리 효과를 섞지 않는다. 실패·시간 초과도 호출 분모에 포함하고 무제한 재시도하지 않는다.
3. gold의 의미 항목과 허용 표현으로 추출을 채점한다. 휴리스틱 채점의 한계를 명시하고 원문에 없는 조건, 필수 조건 누락, 질문 필요 여부를 별도로 확인한다. 이 평가는 모든 자유문장에 대한 의미 증명이 아니다.
4. 같은 AI 초안을 기존 일정 맞춤과 최적화에 각각 넣는다. 순서 변경의 이동 절감과 장소 선택·방문 수 변경을 구분한다. 생성기의 `verified` 플래그만을 정답으로 쓰지 않고 고정 자료의 시간·예약·방문 조건도 별도로 검사한다.
5. 모델명, 실행 시각, BE/AI commit, corpus hash, 입력 수, 총 호출 수, 성공·실패·보완·차단·미확인을 보고한다. 생성된 일정만 골라 성공률을 계산하지 않는다.
6. 사람에게 적용 전후 결과를 보여 주고 순서를 섞어 평가받는다. 실제 제출 수와 평가 항목별 응답 수를 기록한다. 응답이 없으면 효과 수치를 채우지 않는다.

## 충돌 처리

- 명시적으로 서로 다른 도보 상한이 있으면 임의의 상한을 선택하지 않고 기존 `walking:null`, `needsWalkingAnswer:true`로 보완을 요청한다.
- 확인된 서로 다른 장소의 동일 시각 예약, 명시적 시간 구간 중복, 겹치는 숙박은 본인의 기존 `clarifications`에 `SCHEDULE_ITEM_CLARIFICATION_REQUIRED`로 표시한다. 모르는 이동시간이나 체류시간을 만들어 충돌을 단정하지 않는다.
- 지원 태그의 서로 반대인 필수 조건이 겹치는 범위에 적용되면 `REQUIRED_PREFERENCE_UNSATISFIED`로 생성하지 않는다. 모르는 필수 제약은 기존 `REQUIRED_PREFERENCE_UNSUPPORTED`로 남긴다.
- 일반 선호의 차이는 절충 대상으로 남긴다. 선호 점수로 필수 회피 위반을 상쇄하지 않는다. 개인별 원문이나 근거를 공유 readiness/일정에 노출하지 않는다.
- 현재 `likes`에는 필수 중요도 필드가 없다. “자연 명소는 꼭 가야 한다”와 “자연 명소는 반드시 제외”의 동시 입력을 완전하게 표현·해결했다고 주장하지 않는다. 지원되는 필수 음식 선호/회피와 예약 충돌의 검증 범위와 구분한다.
- 예약 희망과 취소를 확정 예약으로 만들지 않는 기존 AI 근거 검사를 유지한다. 날짜·장소가 누락되면 기존 본인 보완 절차를 따른다.

## 최적화 범위

2-opt/relocate의 순서 개선과 TOPTW를 참고한 제한 탐색을 사용한다. TOPTW는 문제 모델이며 이 구현은 제한 시간·이동 조회 횟수 안의 휴리스틱이다. 전역 최적해나 OR-Tools 사용을 주장하지 않는다.

순서 개선은 같은 날짜의 방문 집합을 유지한다. 선택·날짜 배분은 이미 필수 조건을 통과한 후보와 허용 날짜만 사용한다. 예약, 숙소의 다음날 연결, 식사, 도보, 활동 시간, 알려진 비용의 위반과 미확인 악화를 검사한다. 모르는 비용·이동시간을 무료·0분으로 바꾸지 않는다. 예외·예산 소진·나빠진 검증 결과에서는 기존 일정으로 돌아간다. 내부 후보와 지표는 공개 PlanDocument에 추가하지 않는다.

생산 경로에서는 후보의 기존 최저 개인 점수→단체 평균 순위(`selectionRank`)를 사용한다. 이 순위의 합이나 단체 점수는 내부 목적함수이며 실제 만족도 점수가 아니다. 순서·날짜 배분·후보 교체가 제한된 24회 평가 예산을 번갈아 사용하고, 추가 외부 경로 조회는 최대 6회다. 전체 경로 조회 64회·기존 시간 제한을 늘리지 않는다.

## 실제 실행 결과

[2026-11-13 실제 실행 보고서](ai-quality-evaluation-results.md)에 성공·누락·미지원·미확인을 기록했다. 실제 Gemini 평가와 고정 자료의 이동시간 결과를 실제 여행 성과로 표현하지 않는다. 현재 실제 사람 평가 제출 수는 **0건**, 만족도는 **미측정**이다.

DB schema/migration을 변경하지 않으며 공용 DB에 테스트 여행방·개인 정보를 쓰지 않는다. 실시간 Kakao 경로와 브라우저 사용자 인수 검수는 별도 범위다.

## 실행 명령

일반 자동 검사는 실제 Gemini를 호출하지 않는다. 아래 `--live`와 `--schedule-from`만 실제 모델을 호출하므로 명시적으로 실행한다. v1 corpus는 기본 5개·충돌 10개·구성원별 추가 입력을 포함해 파싱 19회이며 일정 초안은 최대 5회다. 자동 재시도는 없다.

```powershell
# 메타데이터와 빈 사람 평가 상태만 생성, 모델 품질 점수를 만들지 않음
.\.venv\Scripts\python.exe scripts/evaluate_ai.py --output .local-dev/evaluation/offline.json
# 실제 Gemini 파싱: 19회
.\.venv\Scripts\python.exe scripts/evaluate_ai.py --live --max-calls 25 --output .local-dev/evaluation/parsing.json
# 저장한 응답 재채점: 추가 모델 호출 0회
.\.venv\Scripts\python.exe scripts/evaluate_ai.py --rescore-from .local-dev/evaluation/parsing.json --output .local-dev/evaluation/rescored.json
# 준비 조건을 통과한 기본 입력만 실제 일정 초안 생성; 동일 초안으로 전후 비교
.\.venv\Scripts\python.exe scripts/evaluate_ai.py --schedule-from .local-dev/evaluation/rescored.json --max-calls 25 --output .local-dev/evaluation/schedules.json
# 저장한 방문 순서의 독립 audit: 추가 모델 호출 0회
.\.venv\Scripts\python.exe scripts/evaluate_ai.py --audit-from .local-dev/evaluation/schedules.json --parsing-from .local-dev/evaluation/rescored.json --output .local-dev/evaluation/audited.json
```

보고서는 `.local-dev/evaluation` 아래에 저장한다. CLI는 출력 경로를 호출 전에 검사한다. 실제 참여자의 설문을 수집할 때는 [사람 평가 빈 양식](../evaluation/human-review-template.csv)을 복사해 사용한다. 입력 원문·개인 설문은 공개 Git에 자동 추가하지 않는다.
