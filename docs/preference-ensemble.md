# Gemini–Qwen 개인 취향 v2

기존 개인 취향 GET/PUT, interpret, confirm에 `X-Preference-Schema: 2`를 보내면 원자 조건을 사용한다. 헤더가 없는 v1 DTO는 유지한다. 원문과 분석 근거는 본인의 private draft에만 저장하며 다른 참여자와 방장에게 공개하지 않는다.

계약 원본: [여행 준비·개인 조건](https://app.notion.com/p/412e977e49088197b345d73603c6a27f). 상세 원자 필드는 AI 저장소 `docs/atomic-contract.md`를 따른다.

## 분석과 저장

`chungbuk_trip_ai.interpret_personal_draft_v2`를 Python 패키지로 호출한다. `PREFERENCE_AI_PROVIDER=ensemble`이면 Gemini와 별도의 로컬 Qwen HTTP 서비스가 같은 원문을 병렬로 분석한다. 일치하지 않는 의미는 unresolved에 남기고, 한 제공자만 응답하면 degraded를 표시한다. 합의는 정답 확률이 아니다.

interpret는 초안을 자동 저장하거나 확정하지 않는다. 첫 결과 저장의 최상위 `analysisReceipt`는 완료된 해석 요청의 `analysisMeta.requestId`다. 서버가 기기 세션·여행방·본인·CV/PV·원문 digest와 완료된 멱등성 cache를 대조한다. 다른 사용자의 영수증과 오래된 결과는 `409 STALE_AI_RESULT`다. 사용자가 변경한 원자는 `origin=user`로 구분한다.

원문·공통 조건이 변경되면 분석 프로필은 stale이 된다. 현재 v2 프로필은 걷기 시간을 임의로 요구하지 않는다. 지원하지 않는 30분 등의 값도 원자 문서에 보존하며 기존 20/40분 값으로 바꾸지 않는다. v1 초안의 기존 준비 조건은 유지한다.

## 단체 인수 경계

현재 버전으로 확정된 참여자의 조건만 사용한다. 한 사람이 같은 조건을 반복해도 지지 수가 증가하지 않는다. 개인의 필수 요청·제한을 다수결로 삭제하지 않고 memberScopedConstraints에 남긴다. 단체 필수 조건, 사람별 제약, 선호 지지 수, 충돌 후보, unresolved 코드와 fingerprint를 내부 생성 캡처에 전달한다. unresolved는 memberRef/code/needsReview만 보존한다. 원문 근거·메시지와 실제 member ID는 전달하지 않는다.

새 원자 조건의 정확한 횟수·날짜 범위를 경로 최적화가 모두 적용하는 것은 후속 담당 범위다. 현재 후보/일정 서비스는 호환 legacy projection도 사용한다. 충돌 후보를 여행 전체의 불가능 판정으로 표현하지 않는다.

## 설치와 검증 범위

BE 실행 환경에 이 기능의 AI 저장소를 editable 설치하고, 별도 서비스의 `QWEN_PREFERENCE_URL`과 서버의 Gemini key를 개인 환경변수로 설정한다. 키를 FE나 Git에 넣지 않는다. AI의 공식 기반 모델·학습 adapter·검증 manifest가 필요하다. 단순 IP나 컨테이너 ID로 학습 모델이 전달되지 않는다.

이번 변경은 기존 draft_json을 사용하며 신규 DDL·migration이 없다. 비 MySQL pytest 229개, 별도 SQLite 실제 HTTP 저장·영수증·확정·stale 검사와 독립 코드 검토를 통과했다. 운영 DB는 테스트/초기화하지 않았다. Docker/WSL 장애로 전용 MySQL 통합 177개는 실행하지 않았다. UI 검수와 실제 모델 비교 결과는 AI 저장소 보고서에 따로 기록한다.
