# 일정 생성 기반: 확정 조건과 AI 구조화

이번 범위는 사용자 요청의 첫 두 단계인 **확정 조건 묶기·장소 DB 매핑**, **확정 취향·제약의 AI 구조화·충돌 후보 반환**입니다. 후보 필터 실행·점수 계산·실제 일정 생성·경로 조회·카카오맵 렌더링·영구 일정 저장은 다음 단계입니다.

기존 [여행 준비·개인 조건](https://app.notion.com/p/412e977e49088197b345d73603c6a27f)과 [공통 규칙](https://app.notion.com/p/40ee977e49088189b512e6e817984d48)을 유지합니다. Notion 목차에는 아직 일정 생성 API 계약이 없으므로 새 HTTP 경로나 공개 DTO를 만들지 않습니다. 기존 6개 준비 API의 동작과 개인정보 접근 범위도 바꾸지 않습니다.

## 확정 조건 캡처

`app/generation_foundation.py`는 내부 호출용 서비스입니다. 인증된 기존 세션과 OWNER·ACTIVE·OPEN을 검증하는 신뢰할 수 있는 callback이 필요합니다. 회원 ID를 인수로 받았다는 이유로 인증을 대신하지 않습니다. 이후 FE 생성 API가 합의되면 그 경계에서 기존 기기 인증을 연결해야 합니다.

캡처는 짧은 트랜잭션에서 여행방 잠금 순서를 지키고 공통 조건, 실제 ACTIVE 참여자 집합, 개인 버전과 현재 두 버전에 대한 전원 확정을 확인합니다. 원본은 migration 002의 공통·개인 문서입니다. 예전 `member_profiles`나 `trip_conditions`에 값을 옮기지 않습니다.

캡처는 중첩 변경이 불가능한 내부 값과 canonical fingerprint로 보관합니다. 개인 원문·닉네임·초대 코드·쿠키는 포함하지 않습니다. 내부 회원/세션 식별자는 권한 재검증에만 사용하며 provider 입력에는 임시 `p1`, `p2` 참조를 사용합니다. AI 처리 중에는 DB 연결과 잠금을 해제하고, 반환 전에 별도 트랜잭션으로 권한·공통 버전·각 개인 버전·실제 참여자 집합·확정 내용을 다시 비교합니다.

이 캡처는 메모리에 존재합니다. DB에 새 snapshot이나 해석 결과를 저장하지 않으며 migration도 추가하지 않습니다. 후속 저장·일정 적용 단계는 이 fingerprint의 현재성을 다시 검사해야 합니다.

## 이전 장소 구조의 읽기 전용 매핑

대상 테이블은 `places`, `place_details`, `place_preferences`입니다. 사용자 지시에 따라 이전 자료의 컬럼 목록과 `contentid INT`를 기준으로 준비했습니다. 공용 DB가 꺼져 있어 실제 최신 DDL을 확인하지 못했으며, 사용자가 추후 요금·영업시간 자료를 업데이트할 예정입니다. CSV 헤더나 격리 fixture를 실제 운영 DDL이라고 표현하지 않습니다.

`app/place_catalog.py`는 전달받은 connection에서 실제 메타데이터를 reflection한 뒤 알려진 컬럼만 읽습니다. 필수 키가 없거나 호환되지 않으면 실패하고, 선택 컬럼 부재는 미확인 정보로 취급합니다. 존재하지 않는 PK·FK나 일대일 관계를 가정하지 않습니다. 자동 DDL·원본 dump 실행·데이터 수정은 없습니다.

- 요금이 누락되면 **요금 확인 필요**입니다. 미상 요금을 0원·무료로 바꾸지 않습니다.
- 영업시간이 누락되면 표시용 영업시간 항목을 생략합니다. 영업 중·24시간으로 추정하지 않습니다.
- `mapx`는 경도, `mapy`는 위도입니다. 이전 파일의 `geoValidated=yes`와 좌표 범위를 검사해 지도용 좌표를 만들고 잘못되거나 미확인인 좌표로 마커를 만들지 않습니다.
- 카카오맵은 WGS84의 `new kakao.maps.LatLng(latitude, longitude)` 순서를 사용합니다. [공식 좌표 문서](https://apis.map.kakao.com/web/documentation/#LatLng)에 맞춰 위도·경도 필드를 구분합니다. `contentid`는 자체 장소 키이며 카카오 장소 ID가 아닙니다.
- 원시 태그 longtext를 임의로 JSON·구분자 배열로 해석하지 않습니다. AI의 지원 태그 사전은 호출자가 의미와 출처를 검증해 명시적으로 전달해야 합니다. 빈 사전도 허용하며 미매핑 항목으로 남깁니다.

## 확정 취향·제약의 AI 구조화

AI 저장소의 `chungbuk-trip-ai` 0.3.0에서 별도 공개 함수 `normalize_confirmed_preferences(confirmed_preferences, supported_tags)`를 제공합니다. 기존 개인 원문 해석 함수와 CLI는 유지합니다.

입력은 확정된 `likes`, `avoid`, `avoidImportance`, `food`, `foodImportance`, `walking`과 임시 참여자 참조뿐입니다. 기존 개인 원문 해석 함수를 다시 호출하지 않습니다. AI는 지원 태그 안에서 근거가 있는 매핑을 제안하며, 중요도는 코드가 확정 입력에서 복사합니다.

반환 내용은 검토용 태그 제안 `terms`, 미매핑·불명확 항목 `unmapped`, 구간별 도보 제한 `walking`, 충돌 후보 `conflictCandidates`, 검토 필요 여부 `needsReview`입니다. 제안을 필수 필터로 자동 채택하거나 사용자 확정 조건을 변경하지 않습니다. 모델이 전체 문장을 근거로 인용해도 의미 전체가 태그로 변환됐다고 단정하지 않으며, 각 확정 문장을 `unmapped`에도 보존합니다. 제안이 있는 필드는 `coverage_unverified`, 제안이 없는 필드는 `uncovered`로 검토 대상으로 남깁니다.

도보 20/40분은 **장소 사이 개별 WALK 구간** 기준입니다. 하루 누적 도보나 차량·대중교통 이동시간으로 해석하지 않습니다. 무제한과 미응답도 구분합니다. 실제 도보시간 조회는 이번 범위가 아닙니다.

충돌은 동일 태그의 선호·회피가 대립하는 등의 내부 검토 후보입니다. 다른 음식 취향이나 서로 다른 도보 한계만으로 여행 불가능을 선언하지 않습니다. 충돌 후보에는 제약 해제 권한이 없으며 타인 개인 정보를 공개하는 HTTP 응답도 없습니다.

## 검증과 후속 연결

AI 검사는 fake HTTPS provider만 사용하고 실제 Google 호출을 하지 않습니다. DB 통합 검사는 기존의 엄격한 전용 `127.0.0.1:3317/osami_entry_test` 환경만 사용합니다. 장소 reflection 검사의 테이블은 synthetic이며 실제 장소 DB의 전체 복제본이 아닙니다. 공용 DB에는 DDL·데이터 쓰기·초기화를 수행하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\python.exe -m compileall -q app tests scripts run.py
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -s ..\WOOWANGTHON_TEAM_17_AI\tests
```

격리 MySQL 검사는 `TEST_DATABASE_URL`을 명시적으로 준비한 경우에만 실행합니다. 실제 검사 수·명령·head와 독립 검토 결과는 연결된 BE/AI PR에 기록합니다.

다음 단계에는 최신 보완 자료와 실제 DB 구조 대조, 지원 태그 매핑 검토, FE의 생성 요청·진행 상태·오류·공유 결과 계약, 후보 필터·점수 계산, 경로 API와 시간 입력 확정, 일정 생성·저장이 필요합니다. 신규 HTTP API 구현 완료나 실제 지도 마커 검수를 뜻하지 않습니다.
