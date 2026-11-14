# Gemini–Qwen 개인 취향 v2

기존 개인 취향 GET/PUT, interpret, confirm에 `X-Preference-Schema: 2`를 보내면 원자 조건을 사용한다. 헤더가 없는 v1 DTO는 유지한다. 원문과 분석 근거는 본인의 private draft에만 저장하며 다른 참여자와 방장에게 공개하지 않는다.

계약 원본: [여행 준비·개인 조건](https://app.notion.com/p/412e977e49088197b345d73603c6a27f). 상세 원자 필드는 AI 저장소 `docs/atomic-contract.md`를 따른다.

## 분석과 저장

`chungbuk_trip_ai.interpret_personal_draft_v2`를 Python 패키지로 호출한다. `PREFERENCE_AI_PROVIDER=ensemble`이면 Gemini와 별도의 로컬 Qwen HTTP 서비스가 같은 원문을 병렬로 분석한다. 일치하지 않는 의미는 unresolved에 남기고, 한 제공자만 응답하면 degraded를 표시한다. 합의는 정답 확률이 아니다.

interpret는 초안을 자동 저장하거나 확정하지 않는다. 첫 결과 저장의 최상위 `analysisReceipt`는 완료된 해석 요청의 `analysisMeta.requestId`다. 서버가 기기 세션·여행방·본인·CV/PV·원문 digest와 완료된 멱등성 cache를 대조한다. 다른 사용자의 영수증과 오래된 결과는 `409 STALE_AI_RESULT`다. 사용자가 변경한 원자는 `origin=user`로 구분한다.

원문·공통 조건이 변경되면 분석 프로필은 stale이 된다. 현재 v2 프로필은 걷기 시간을 임의로 요구하지 않는다. 지원하지 않는 30분 등의 값도 원자 문서에 보존하며 기존 20/40분 값으로 바꾸지 않는다. v1 초안의 기존 준비 조건은 유지한다.

v2 개인 GET/PUT은 선택 필드 `unresolvedAcknowledgements`를 응답 최상위에 제공하고 PUT 최상위에서 받을 수 있다. 형식은 `{sourceDigest, items:[{note:{code,evidence,message}, disposition:"handled"|"ignored"}]}`이다. 서버는 현재 원문·공통 조건 digest와 동일한 신뢰된 profile의 unresolved note 세 항목을 정확히 대조한다. 중복·수정·알 수 없는 note나 오래된 digest는 거부한다. 빈 `items`는 기존 확인을 초기화한다. 확인은 사용자가 note를 검토했다는 표시이며 note를 지우거나 모델 판단의 정확성·제약 충족을 보증하지 않는다. 생략하면 같은 source의 기존 확인만 유지한다. 이 private 메타데이터는 기존 `draft_json`에 atomicPreferences 바깥으로 저장하며 v1 및 단체 응답에 내보내지 않는다. source 변경 시 효력을 잃고, 확인만 저장해도 원자 프로필의 stale 상태를 해제하지 않는다.

## 단체 인수 경계

현재 버전으로 확정된 참여자의 조건만 사용한다. 한 사람이 같은 조건을 반복해도 지지 수가 증가하지 않는다. 개인의 필수 요청·제한을 다수결로 삭제하지 않고 memberScopedConstraints에 남긴다. 단체 필수 조건, 사람별 제약, 선호 지지 수, 충돌 후보, unresolved 코드와 fingerprint를 내부 생성 캡처에 전달한다. unresolved는 memberRef/code/needsReview만 보존한다. 원문 근거·메시지와 실제 member ID는 전달하지 않는다.

현재 원자 프로필을 가진 참여자는 내부 `atomicMemberRefs`로 식별한다. 원자가 비어 있어도 이전 호환 문구를 다시 필수 조건으로 해석하지 않는다. v1 참여자의 기존 조건 분석은 유지한다.

`route_preferences.compile_route_preferences`는 확정 단체 조건을 카탈로그에 연결해 장소 점수, 여행 전체·매일의 횟수 조건, 수치 도보 상한으로 변환한다. 필수 음식·활동 요청은 일정의 방문 횟수로 검사한다. 같은 음식 태그를 모든 식당에 요구하지 않는다. 선호와 중요도 미지정 조건은 후보 점수와 일정의 목표 횟수 오차에 반영한다.

초안의 `scheduleContext.preferenceRules`에는 후보 contentid 목록, `trip|day`, `exact|min|max|target`, 횟수, 필수 여부만 전달한다. 실제 member ID·원문·근거·모델별 응답을 보내지 않는다. 최종 검사와 ALNS·2-opt·relocate·날짜 재배분의 평가에서 같은 컴파일 결과를 사용한다. 필수 위반 또는 필수 조건의 확인 불가가 남으면 생성 성공·계획 영속화로 진행하지 않는다. 기존 실패 코드 `REQUIRED_PREFERENCE_UNSUPPORTED`와 `REQUIRED_PREFERENCE_UNSATISFIED`를 사용한다.

별도 대상이 없는 관광 방문 수는 `sightseeing_stop|culture_stop|activity_stop|shopping_stop|date_specific_event`를 센다. 식사와 숙박은 그 수에서 제외하며 식사 수는 `meal_stop`를 센다. 날짜가 지정된 예약은 기존 `scheduleItems`와 검토 앵커로 전달한다. 원자 schema v2에 개별 날짜 필드가 없어 ‘둘째 날만’ 같은 `segment` 조건을 임의로 매일 적용하지 않는다. 필요한 개별 날짜를 표현할 수 없는 필수 원자는 미지원으로 남긴다.

카탈로그의 정확한 태그로 확인할 수 있는 양의 일치만 활용한다. 태그 미표시는 부재·알레르기 안전의 증거가 아니며 제목·임베딩으로 이를 증명하지 않는다. 근사 필수 횟수에 임의 허용 오차를 만들지 않는다. 도보는 확인된 `walkingSeconds`로 하루·여행 전체 누적 또는 원문에 명시된 연속 구간을 검사하며 알 수 없는 경로 시간을 0으로 바꾸지 않는다. 최적화 실패는 해당 제한 탐색의 결과이며 여행 전체의 불가능 판정이 아니다.

## 설치와 검증 범위

2026-11-14 경로 인수 연결은 최신 경로 알고리즘 main `1be929e`와 기존 v2 개인 검토 작업을 함께 검사했다. BE non-MySQL pytest 426개, AI pytest 127개와 87 subtests를 통과했다. BE MySQL 177개는 선택에서 제외했고 AI 학습 의존성 검사 2개는 skip했다. 필수 횟수의 후보 보존·날짜별 고유 후보 배정·반복 예약·알 수 없는 태그/도보·최적화 위험·저장 전 거부 및 기존 v1 동작을 합성 자료와 모의 provider로 확인했다. 독립 코드 검토의 중요 지적을 수정하고 관련 회귀 검사를 추가했다. 실제 Gemini·Docker Qwen·Kakao·운영 MySQL을 함께 쓰는 인수는 이 변경에서 실행하지 않았다.

BE 실행 환경에 이 기능의 AI 저장소를 editable 설치하고, 별도 서비스의 `QWEN_PREFERENCE_URL`과 서버의 Gemini key를 개인 환경변수로 설정한다. 키를 FE나 Git에 넣지 않는다. AI의 공식 기반 모델·학습 adapter·검증 manifest가 필요하다. 단순 IP나 컨테이너 ID로 학습 모델이 전달되지 않는다.

이번 변경은 기존 draft_json을 사용하며 신규 DDL·migration이 없다. 비 MySQL pytest 229개, 별도 SQLite 실제 HTTP 저장·영수증·확정·stale 검사와 독립 코드 검토를 통과했다. 운영 DB는 테스트/초기화하지 않았다. Docker/WSL 장애로 전용 MySQL 통합 177개는 실행하지 않았다. UI 검수와 실제 모델 비교 결과는 AI 저장소 보고서에 따로 기록한다.
