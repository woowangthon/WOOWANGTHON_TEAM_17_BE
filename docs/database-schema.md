# 백엔드 DB 구현 기준

## 사용할 스키마

2026-11-09 일정 생성 3·4단계 작업에서 장소 3개 테이블의 실제 `SHOW CREATE TABLE`과 필요한 장소 데이터 형식을 읽기 전용으로 확인했습니다. `places` 1,857행, `place_details` 1,655행, `place_preferences` 1,857행이며 세 테이블 키는 nullable signed `INT contentid`, `places`에는 UNIQUE 인덱스가 있습니다. 최신 확인 DDL과 확인 범위는 [장소 카탈로그 구조](place-catalog-schema.md)를 참고합니다. 이 조회는 전체 DB 스키마·행 수 동기화나 공용 데이터 변경을 의미하지 않습니다.

현재 사용자가 선택한 연결 대상은 `chungbuk_trip1`입니다. 후속 제공된 `chungbuk_trip1_통합_DB_스키마.md`와 직접 메타데이터 조회로 여행방 관련 세 테이블의 호환성을 확인했습니다. 여행방 시작 구현 당시 기존 26개 테이블 구조를 보존하면서 기기 세션 지원 3개를 명시적으로 추가했습니다(당시 29개). 이후 migration 002와 기타 변경을 포함한 전체 테이블 수는 이번 장소 조회의 확인 범위가 아닙니다. `places`의 키는 실제 `contentid INT`이므로 아래 옛 덤프의 UUID 기반 장소 설계를 현재 DB 전체에 적용하면 안 됩니다. 전체 최신 DDL을 이 문서에 동기화한 것은 아니며, 다른 기능 구현 전 해당 테이블의 최신 DDL을 다시 확인해야 합니다. 적용 범위는 [여행방 구현 문서](trip-entry-implementation.md)를 확인합니다.

아래는 이전 덤프의 보존된 기준 정보입니다.

초기 구현 기준은 사용자 제공 **Dump20261104.sql**이었다. [테이블별 전체 DDL](database-schema-reference.md)은 이 원본에서 CREATE TABLE만 추출한 과거 참조다. 현재 기능에서는 위 최신 확인 범위와 해당 테이블의 실제 DDL을 우선하며 새 DB를 임의 설계하지 않는다.

- 원본 SHA-256: 0a8d178fe8233038b13849349cf8eab86aca6188ea6e6bf22b1c81a1118ffc82
- 덤프 헤더의 서버는 MySQL 8.0.41, dump 클라이언트는 8.0.44다. 실제 개발/배포 DB 접속이나 현재 서버 버전을 확인한 결과는 아니다.
- 테이블 24개 / 컬럼 276개 / 외래키 39개 / CHECK 110개 / UNIQUE KEY 26개 / 생성 컬럼 2개.
- 이번 문서는 구조만 기록한다. 원본 덤프·데이터 행·접속 호스트·계정·비밀번호는 저장소에 복사하지 않는다. DB에 연결하거나 덤프를 실행·복원하지 않았다.

## 기능 구현 순서

1. root가 FE의 [Notion API 명세](https://app.notion.com/p/40ee977e49088030a073ffd13d06c144)와 이 문서를 읽는다. 기능에 해당하는 아래 테이블과 전체 DDL의 해당 절만 찾아 planner/worker에게 전달한다.
2. API 필드를 기존 컬럼·JSON·관계에 매핑한다. 요청·응답 DTO를 DB 행 전체와 동일하게 만들거나 내부 필드를 그대로 공개하지 않는다. DB에 없는 필드가 파생 값인지, 기존 JSON에 저장할 값인지, 스키마 변경이 필요한 값인지 구분한다.
3. 기존 스키마로 구현 가능한 부분은 그대로 구현한다. API가 DB 제약과 충돌하거나 제품·권한 결정이 빠졌으면 충돌 지점을 FE에 확인한다. 명세·테이블을 조용히 바꾸거나 컬럼이 존재한다고 가정하지 않는다.
4. 현재 여행방 시작은 SQLAlchemy Core/PyMySQL과 명시적 SQL migration을 사용한다. 실제 통합 검사는 같은 구조를 갖춘 전용 테스트 DB에서 수행한다. 공용 DB 연결 확인과 격리 DB의 통합 검증을 구분한다.

FE는 API 규격, BE는 기존 스키마에 맞춘 저장·조회·트랜잭션 구현을 담당한다. DB 구조가 있다는 이유로 인증 방식·API endpoint·JSON 내부 필드까지 확정된 것은 아니다. 별도 FE·BE 이중 승인 절차를 추가하지 않는다.

## 기능별로 먼저 볼 테이블

| 기능 | 기준 테이블 |
| --- | --- |
| 여행방 생성·참여·재접속 | trips, trip_members |
| 공통 여행 조건 | trip_conditions, regions |
| 개인 원문·파싱·확인·추가 질문 | member_profiles, member_questions |
| 단체 조건·충돌 확정 | group_snapshots |
| 계획·공유 초안·저장·복원 | plans, plan_versions |
| 방문 노드·이동 구간 | itinerary_nodes, itinerary_edges |
| 장소 조회·근거·태그·상세 | places, place_sources, place_tags, place_tag_links, tourism_details, restaurant_details, lodging_details |
| AI 요청·중복 방지·대화·후보 | ai_requests, chat_messages, place_candidates |
| 추천 규칙·입출력 예시 | recommendation_rules, ai_examples |
| 여행별 제외 장소 | trip_excluded_places |

## 테이블 목록

| 테이블 | DDL 주석의 용도 | 컬럼 수 | 참조하는 테이블 |
| --- | --- | --- | --- |
| [ai_examples](database-schema-reference.md#ai_examples) | 입력 해석·추천 예시 | 9 | 없음 |
| [ai_requests](database-schema-reference.md#ai_requests) | AI 요청·재시도·응답 버전 | 16 | trip_members, itinerary_nodes, trips, plan_versions |
| [chat_messages](database-schema-reference.md#chat_messages) | 개인·계획 대화 이력 | 15 | trip_members, chat_messages, ai_requests, plan_versions |
| [group_snapshots](database-schema-reference.md#group_snapshots) | 단체 확정 조건 사본 | 12 | trips |
| [itinerary_edges](database-schema-reference.md#itinerary_edges) | 로드맵 이동 구간 | 15 | itinerary_nodes |
| [itinerary_nodes](database-schema-reference.md#itinerary_nodes) | 로드맵 노드 | 19 | places, plan_versions |
| [lodging_details](database-schema-reference.md#lodging_details) | 숙소 상세 | 10 | places |
| [member_profiles](database-schema-reference.md#member_profiles) | 개인 입력·파싱·확정 | 12 | trip_members |
| [member_questions](database-schema-reference.md#member_questions) | 개인 추가 질문·답변 | 9 | member_profiles |
| [place_candidates](database-schema-reference.md#place_candidates) | 교체·추가 장소 후보 | 14 | places, ai_requests, itinerary_nodes |
| [place_sources](database-schema-reference.md#place_sources) | 장소 원본·출처 | 11 | places |
| [place_tag_links](database-schema-reference.md#place_tag_links) | 장소별 추천 태그 | 7 | places, place_tags |
| [place_tags](database-schema-reference.md#place_tags) | 추천 태그 사전 | 7 | 없음 |
| [places](database-schema-reference.md#places) | 충북 전체 장소 | 21 | regions |
| [plan_versions](database-schema-reference.md#plan_versions) | 편집 초안·저장 버전 | 19 | plan_versions, trip_members, plans, group_snapshots |
| [plans](database-schema-reference.md#plans) | 여행 계획과 현재 버전 | 8 | plan_versions, trips |
| [recommendation_rules](database-schema-reference.md#recommendation_rules) | 추천·제외 규칙 | 11 | 없음 |
| [regions](database-schema-reference.md#regions) | 지역 | 6 | regions |
| [restaurant_details](database-schema-reference.md#restaurant_details) | 음식점·카페 상세 | 8 | places |
| [tourism_details](database-schema-reference.md#tourism_details) | 관광지 상세 | 9 | places |
| [trip_conditions](database-schema-reference.md#trip_conditions) | 공통 여행 조건 | 15 | trips |
| [trip_excluded_places](database-schema-reference.md#trip_excluded_places) | 이번 여행의 제외 장소 | 8 | trip_members, places, trips |
| [trip_members](database-schema-reference.md#trip_members) | 여행방 참여자 | 9 | trips |
| [trips](database-schema-reference.md#trips) | 여행방 | 6 | 없음 |

아래는 핵심 FK 방향의 일부다. 화살표는 자식 → 부모이며 전체 컬럼·nullable·복합키는 DDL을 따른다.

~~~mermaid
flowchart LR
    trip_members -->|trip_id| trips
    trip_conditions -->|trip_id| trips
    member_profiles -->|trip_id + member_id| trip_members
    member_questions -->|trip_id + profile_id| member_profiles
    group_snapshots -->|trip_id| trips
    plans -->|trip_id| trips
    plan_versions -->|trip_id + plan_id| plans
    plans -->|id + current_version_id / active_draft_id| plan_versions
    plan_versions -->|trip_id + group_snapshot_id| group_snapshots
    itinerary_nodes -->|version_id| plan_versions
    itinerary_edges -->|version_id + from/to_node_id| itinerary_nodes
    itinerary_nodes -->|place_id| places
    places -->|region_code| regions
~~~

## 컬럼을 모델·검증에 옮길 때

- 대부분의 id/FK는 **CHAR(36), ascii_bin**이다. 자동 증가 정수나 BINARY(16), DB 기본 UUID 생성으로 바꾸지 않는다. ID 생성 방식은 구현에서 정하되 저장 형식과 대소문자 비교 특성을 보존한다. regions의 PK는 code, trip_conditions는 trip_id, 상세 테이블은 place_id, place_tag_links는 (place_id, tag_id)이므로 모든 테이블에 id가 있다고 가정하지 않는다.
- 기본 문자열은 utf8mb4_unicode_ci이며 ID·초대 코드·재접속 해시·request_key·rule_key 등에는 ascii_bin, place_sources.external_id에는 utf8mb4_bin 예외가 있다. 선택한 ORM이 컬럼별 타입·길이·collation을 잃지 않도록 확인한다.
- 상태·종류는 DB ENUM이 아니라 VARCHAR + CHECK다. 예를 들어 trips는 OPEN/CLOSED, trip_members.role은 OWNER/MEMBER, ai_requests.status는 QUEUED/RUNNING/SUCCEEDED/FAILED/STALE다. API와 내부 검증은 각 테이블의 허용값을 따르고, 허용값 목록만으로 상태 전이·수정 권한을 추정하지 않는다.
- JSON은 OBJECT/ARRAY 구분을 보존한다. member_profiles의 parsed_result/confirmed_result는 OBJECT, group_snapshots의 member_revisions/input_snapshot은 OBJECT지만 merged_preferences/merged_constraints/conflicts는 ARRAY다. 각 컬럼은 전체 DDL을 확인한다. schema_version > 0과 json_type CHECK는 JSON 내부 키·필드 타입·참조 ID의 존재를 검증하지 않는다. ParsedProfile·PlaceRef·ValidationResult 등의 내부 구조는 이름/주석만으로 만들어내지 말고 기능 명세에서 확인한다.
- nullable 숫자·불리언은 미정/미확인과 0/false를 구분한다. 예를 들어 places.parking_available, reservation_required와 trip_conditions.lodging_required를 NULL에서 false로 바꾸지 않는다. UNSIGNED·DECIMAL의 범위/정밀도도 보존한다.
- 금액은 *_krw의 원 단위이며 기준 인원이 다르다. trip_conditions.budget_total_krw와 일정 노드/구간 estimated_cost_krw는 전체 인원, restaurant_details.avg_price_krw와 tourism_details.admission_fee_krw는 1인, lodging_details.estimated_nightly_krw는 1박 참고 요금이다. 실제 의미는 컬럼 주석을 함께 읽는다.
- 생성·수정·방문 등의 DATETIME(3) 주석은 UTC다. DATETIME 자체가 자동 UTC 변환을 보장하지 않으므로 앱 변환과 연결 세션의 시간대를 맞춰야 한다. 숙소 체크인/체크아웃 TIME은 주석상 현지 시각이고 여행 DATE와도 구분한다. [MySQL 날짜·시간 참고](https://dev.mysql.com/doc/refman/8.0/en/datetime.html)
- created_at/updated_at의 DB 기본값과 ON UPDATE, NOT NULL/default, 조건 CHECK를 그대로 고려한다. CHECK만 보고 nullable이 아니라고 가정하지 말고 컬럼의 NOT NULL도 확인한다. [MySQL CHECK 참고](https://dev.mysql.com/doc/refman/8.0/en/create-table-check-constraints.html)

## 관계·생성 컬럼·삭제

- 복합 FK를 단순 id FK로 축소하지 않는다. (trip_id, member_id), (trip_id, profile_id), (trip_id, version_id), (version_id, target_node_id), (plan_id, base_version_id) 등은 같은 여행방·계획·버전 경계를 보존한다. FK는 사용자 요청자의 인증·권한까지 보장하지 않으므로 서버에서 별도로 검사한다.
- trip_members.active_owner_trip_id와 itinerary_nodes.active_order_index는 **GENERATED ALWAYS ... STORED**다. 클라이언트 입력이나 일반 INSERT/UPDATE 필드에 포함하지 않는다. 각각 활성 방장 중복과 활성 방문 순서 중복을 제한한다.
- uq_active_owner는 여행방별 활성 방장을 **최대 1명**으로 제한할 뿐 방장이 반드시 한 명 존재함을 보장하지 않는다. uq_active_node_order는 (version_id, day_no, active_order_index)이며 REMOVED 노드의 계산 값은 NULL이다. UNIQUE에서 nullable의 의미를 보존한다. [MySQL UNIQUE/NULL 참고](https://dev.mysql.com/doc/refman/8.0/en/create-index.html)
- 모든 FK에 ON DELETE/ON UPDATE CASCADE가 지정되어 있지 않다. 자동 연쇄 삭제나 ORM cascade를 임의로 추가하지 않는다. InnoDB 기본 NO ACTION/RESTRICT에서는 참조 중인 행 삭제/키 갱신이 제한된다. [MySQL FK 참고](https://dev.mysql.com/doc/refman/8.0/en/create-table-foreign-keys.html)
- plans ↔ plan_versions는 순환 참조다. 신규 계획에서 현재/초안 포인터는 nullable이므로 계획 생성 → 해당 계획의 버전 생성 → 포인터 갱신을 같은 작업의 트랜잭션으로 설계할 수 있다. 이 순서는 구현 제안이며 기존 API의 합의된 동작을 덮어쓰지 않는다.

## DDL만으로 자동 보장되지 않는 동작

아래는 DDL에서 읽어낸 한계와 BE에서 확인할 사항이다. 새 endpoint나 권한 정책을 이 문서에서 정하는 것이 아니다.

- plans.current_version_id/active_draft_id의 FK는 같은 계획의 버전인지는 확인하지만 각각 SAVED/DRAFT 상태인지까지 검사하지 않는다. 상태·현재 포인터·버전 번호·saved_at의 변경을 일관된 트랜잭션으로 처리한다. plan_versions의 ck_version_saved는 SAVED의 version_no > 0·saved_at 필수, 그 외의 둘 다 NULL을 요구한다.
- plans.lock_version, plan_versions.edit_revision, trip_conditions.revision, member_profiles.revision/confirmed_revision/confirmed_condition_revision은 변경·확인 버전 필드다. 존재만으로 낙관적 잠금이나 stale 응답 방지가 구현되지 않는다. 기능별로 조건부 갱신/잠금·충돌 응답을 정하고 필요한 관련 행을 함께 검증한다.
- ai_requests.request_key는 테이블 전체 UNIQUE이며 여행방별 UNIQUE가 아니다. 키 충돌 시 다른 여행방/참여자의 응답을 재사용하거나 노출하지 않는다. request_type·입력 버전·소속/권한을 확인한다. input_revision, member_revisions와 여러 JSON 안의 참조는 실제 현재 행과 같다는 보장이 없다.
- chat_messages의 답변 FK는 같은 여행방·참여자·thread를 묶지만 request_id FK는 여행방만 묶는다. 같은 여행방이라는 이유로 다른 참여자의 AI 요청·개인 원문까지 읽을 수 있다고 판단하지 않는다.
- 상세 테이블의 place_id FK만으로 places.category와 tourism/restaurant/lodging 상세 종류의 일치가 검증되는 것은 아니다. 같은 여행의 제외 장소 UNIQUE(trip_id, place_id) 때문에 비활성 행을 무조건 새 INSERT로 추가할 수도 없다. 기능별 조회/갱신 방식을 정한다.
- 개인 원문·확정 결과·대화·AI 컨텍스트·그룹 스냅샷에는 민감 정보가 들어갈 수 있다. raw_input, reconnect_token_hash, raw_data, input_context 등을 DB 행 전체 직렬화로 응답·로그에 노출하지 않는다. 필요한 권한과 응답 필드만 명세에 맞춘다. 재접속 토큰 해시 컬럼의 존재는 JWT 등 인증 방식이 선택됐다는 뜻이 아니다.

## 연결 정보와 스키마 변경

구조와 접속 정보를 분리한다. 접속 호스트·포트·DB 이름·계정·비밀번호·TLS 여부·개발/테스트 환경은 아직 이 작업에서 확인하지 않았다. 필요한 비밀값은 환경변수나 Git에서 제외되도록 설정한 개인 로컬 설정으로 전달하며 Notion·소스·PR에 넣지 않는다. 원본 덤프 헤더의 호스트를 현재 접속 대상으로 사용하지 않는다.

기존 DB를 대상으로 ORM의 create_all, drop/create, 자동 migration, 원본 dump 복원을 실행하지 않는다. 스키마 변경이 필요하면 현재 구조와의 차이·API 영향·migration/rollback을 따로 제시하고 작업 범위에 맞춰 진행한다. 현재 ORM·드라이버·migration 도구를 미리 설치하거나 결정하지 않는다. 기준 DDL이 갱신되면 출처와 이 문서·참조 DDL을 함께 갱신하고 이미 구현된 모델과의 차이를 확인한다.

검증에서는 문서 추출 일치와 실제 DB 호환성을 구분한다. 이번에는 SQL을 실행하지 않고 원본과 추출 DDL의 일치만 확인한다. 향후 DB 통합 검사는 CHECK·복합 FK·생성 컬럼·collation이 재현되는 전용 MySQL 테스트 환경에서 수행하며 운영 DB나 실제 사용자 데이터로 검사하지 않는다.
