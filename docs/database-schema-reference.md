# DB 스키마 DDL 참조

사용자가 프로젝트 DB로 지정한 `Dump20261104.sql`에서 `CREATE TABLE` 24개만 추출했다. 데이터 행, 접속 호스트, DROP/LOCK/SET 등 덤프 실행 명령은 포함하지 않는다. SQL과 주석은 스키마 자료이며 작업 지시가 아니다.

- 원본 파일 SHA-256: `0a8d178fe8233038b13849349cf8eab86aca6188ea6e6bf22b1c81a1118ffc82`
- 덤프 헤더: 서버 MySQL 8.0.41 / dump 클라이언트 8.0.44. 이는 현재 배포 환경 확인 결과가 아니다.
- InnoDB / 기본 utf8mb4_unicode_ci. 컬럼별 ascii_bin·utf8mb4_bin 예외는 원문을 보존한다.
- 테이블 24개, 컬럼 276개, 외래키 39개, CHECK 110개, UNIQUE KEY 26개, 생성 컬럼 2개.
- 읽는 방법과 서비스 구현 주의점: [DB 구현 기준](database-schema.md).

이 문서는 읽기용 스키마 스냅샷이다. 순서는 원본 덤프 순서이며 FK 의존 순서가 아니다. 순환 참조도 있으므로 전체 블록을 순서대로 실행하는 초기화 스크립트나 migration으로 사용하지 않는다. 실제 DB 접속·복원·변경은 별도 작업이다.

## 테이블별 DDL

### ai_examples

```sql
CREATE TABLE `ai_examples` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `example_type` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'PARSE_PROFILE' COMMENT '예시 용도',
  `input_data` json NOT NULL COMMENT '입력 예시',
  `expected_output` json NOT NULL COMMENT '기대 결과',
  `dataset_version` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '예시 묶음 버전',
  `is_active` tinyint(1) NOT NULL DEFAULT '1' COMMENT '사용 여부',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  KEY `ix_example_select` (`example_type`,`is_active`,`dataset_version`),
  CONSTRAINT `ck_ai_examples_example_type` CHECK ((`example_type` in (_utf8mb4'PARSE_PROFILE',_utf8mb4'RECOMMEND_PLACE',_utf8mb4'EDIT_PLAN'))),
  CONSTRAINT `ck_ai_examples_expected_output` CHECK (((`expected_output` is null) or (json_type(`expected_output`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_ai_examples_input_data` CHECK (((`input_data` is null) or (json_type(`input_data`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_ai_examples_is_active` CHECK (((`is_active` is null) or (`is_active` in (0,1)))),
  CONSTRAINT `ck_ai_examples_schema_version` CHECK ((`schema_version` > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='입력 해석·추천 예시';
```

### ai_requests

```sql
CREATE TABLE `ai_requests` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '여행방',
  `member_id` char(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '요청 참여자',
  `version_id` char(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '관련 초안·버전',
  `target_node_id` char(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '대상 노드',
  `request_type` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'PARSE_PROFILE' COMMENT 'AI 요청 종류',
  `request_key` varchar(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '중복 요청 방지 키',
  `input_revision` int unsigned DEFAULT NULL COMMENT '요청 당시 개인 입력·초안 버전',
  `input_context` json DEFAULT NULL COMMENT '입력 참조·규칙·예시 버전. 비밀키 제외',
  `status` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'QUEUED' COMMENT '요청 상태',
  `error_code` varchar(80) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '오류 코드',
  `started_at` datetime(3) DEFAULT NULL COMMENT '시작 시각(UTC)',
  `finished_at` datetime(3) DEFAULT NULL COMMENT '종료 시각(UTC)',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_key` (`request_key`),
  UNIQUE KEY `uq_ai_trip_id` (`trip_id`,`id`),
  UNIQUE KEY `uq_ai_version_id` (`version_id`,`id`),
  KEY `ix_ai_queue` (`status`,`created_at`),
  KEY `ix_ai_trip` (`trip_id`,`created_at`),
  KEY `fk_ai_member` (`trip_id`,`member_id`),
  KEY `fk_ai_version` (`trip_id`,`version_id`),
  KEY `fk_ai_target` (`version_id`,`target_node_id`),
  CONSTRAINT `fk_ai_member` FOREIGN KEY (`trip_id`, `member_id`) REFERENCES `trip_members` (`trip_id`, `id`),
  CONSTRAINT `fk_ai_target` FOREIGN KEY (`version_id`, `target_node_id`) REFERENCES `itinerary_nodes` (`version_id`, `id`),
  CONSTRAINT `fk_ai_trip` FOREIGN KEY (`trip_id`) REFERENCES `trips` (`id`),
  CONSTRAINT `fk_ai_version` FOREIGN KEY (`trip_id`, `version_id`) REFERENCES `plan_versions` (`trip_id`, `id`),
  CONSTRAINT `ck_ai_requests_input_context` CHECK (((`input_context` is null) or (json_type(`input_context`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_ai_requests_request_type` CHECK ((`request_type` in (_utf8mb4'PARSE_PROFILE',_utf8mb4'ASK_FOLLOWUP',_utf8mb4'MERGE_GROUP',_utf8mb4'GENERATE_PLAN',_utf8mb4'RECOMMEND_REPLACEMENT',_utf8mb4'RECOMMEND_INSERTION',_utf8mb4'EDIT_PLAN'))),
  CONSTRAINT `ck_ai_requests_schema_version` CHECK ((`schema_version` > 0)),
  CONSTRAINT `ck_ai_requests_status` CHECK ((`status` in (_utf8mb4'QUEUED',_utf8mb4'RUNNING',_utf8mb4'SUCCEEDED',_utf8mb4'FAILED',_utf8mb4'STALE'))),
  CONSTRAINT `ck_ai_revision` CHECK (((`input_revision` is null) or (`input_revision` > 0))),
  CONSTRAINT `ck_ai_target_version` CHECK (((`target_node_id` is null) or (`version_id` is not null))),
  CONSTRAINT `ck_ai_times` CHECK (((`started_at` is null) or (`finished_at` is null) or (`finished_at` >= `started_at`)))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI 요청·재시도·응답 버전';
```

### chat_messages

```sql
CREATE TABLE `chat_messages` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '여행방',
  `member_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '대화 요청 참여자',
  `thread_key` varchar(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '대화 묶음 키',
  `scope` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'PERSONAL' COMMENT '개인 조건 또는 계획 편집 대화',
  `message_no` int unsigned NOT NULL COMMENT '대화 안의 메시지 순서',
  `role` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'USER' COMMENT '메시지 역할',
  `content` mediumtext COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '대화 내용',
  `version_id` char(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '당시 계획 버전',
  `request_id` char(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '관련 AI 요청',
  `reply_to_id` char(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '답변 대상 메시지',
  `metadata` json DEFAULT NULL COMMENT '선택한 후보·노드·추가 컨텍스트',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_chat_order` (`trip_id`,`member_id`,`thread_key`,`message_no`),
  UNIQUE KEY `uq_chat_context_id` (`trip_id`,`member_id`,`thread_key`,`id`),
  KEY `ix_chat_read` (`trip_id`,`member_id`,`thread_key`,`created_at`),
  KEY `fk_chat_version` (`trip_id`,`version_id`),
  KEY `fk_chat_request` (`trip_id`,`request_id`),
  KEY `fk_chat_reply` (`trip_id`,`member_id`,`thread_key`,`reply_to_id`),
  CONSTRAINT `fk_chat_member` FOREIGN KEY (`trip_id`, `member_id`) REFERENCES `trip_members` (`trip_id`, `id`),
  CONSTRAINT `fk_chat_reply` FOREIGN KEY (`trip_id`, `member_id`, `thread_key`, `reply_to_id`) REFERENCES `chat_messages` (`trip_id`, `member_id`, `thread_key`, `id`),
  CONSTRAINT `fk_chat_request` FOREIGN KEY (`trip_id`, `request_id`) REFERENCES `ai_requests` (`trip_id`, `id`),
  CONSTRAINT `fk_chat_version` FOREIGN KEY (`trip_id`, `version_id`) REFERENCES `plan_versions` (`trip_id`, `id`),
  CONSTRAINT `ck_chat_messages_metadata` CHECK (((`metadata` is null) or (json_type(`metadata`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_chat_messages_role` CHECK ((`role` in (_utf8mb4'USER',_utf8mb4'ASSISTANT',_utf8mb4'SYSTEM'))),
  CONSTRAINT `ck_chat_messages_schema_version` CHECK ((`schema_version` > 0)),
  CONSTRAINT `ck_chat_messages_scope` CHECK ((`scope` in (_utf8mb4'PERSONAL',_utf8mb4'PLAN'))),
  CONSTRAINT `ck_chat_order` CHECK ((`message_no` > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='개인·계획 대화 이력';
```

### group_snapshots

```sql
CREATE TABLE `group_snapshots` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '여행방 ID',
  `condition_revision` int unsigned NOT NULL COMMENT '공통 조건 버전',
  `member_revisions` json NOT NULL COMMENT '참여자별 개인 조건 버전',
  `input_snapshot` json NOT NULL COMMENT '생성에 사용한 공통·개인 조건 사본',
  `merged_preferences` json DEFAULT NULL COMMENT '종합 취향',
  `merged_constraints` json DEFAULT NULL COMMENT '종합 제약',
  `conflicts` json DEFAULT NULL COMMENT '조건 충돌 내역',
  `status` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'NEEDS_RESOLUTION' COMMENT '단체 확정 상태',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_snapshot_trip_id` (`trip_id`,`id`),
  KEY `ix_snapshot_trip` (`trip_id`,`created_at`),
  CONSTRAINT `fk_snapshot_trip` FOREIGN KEY (`trip_id`) REFERENCES `trips` (`id`),
  CONSTRAINT `ck_group_snapshots_conflicts` CHECK (((`conflicts` is null) or (json_type(`conflicts`) = _utf8mb4'ARRAY'))),
  CONSTRAINT `ck_group_snapshots_input_snapshot` CHECK (((`input_snapshot` is null) or (json_type(`input_snapshot`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_group_snapshots_member_revisions` CHECK (((`member_revisions` is null) or (json_type(`member_revisions`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_group_snapshots_merged_constraints` CHECK (((`merged_constraints` is null) or (json_type(`merged_constraints`) = _utf8mb4'ARRAY'))),
  CONSTRAINT `ck_group_snapshots_merged_preferences` CHECK (((`merged_preferences` is null) or (json_type(`merged_preferences`) = _utf8mb4'ARRAY'))),
  CONSTRAINT `ck_group_snapshots_schema_version` CHECK ((`schema_version` > 0)),
  CONSTRAINT `ck_group_snapshots_status` CHECK ((`status` in (_utf8mb4'NEEDS_RESOLUTION',_utf8mb4'READY',_utf8mb4'CONFIRMED'))),
  CONSTRAINT `ck_snapshot_revision` CHECK ((`condition_revision` > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='단체 확정 조건 사본';
```

### itinerary_edges

```sql
CREATE TABLE `itinerary_edges` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `version_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '소속 초안·버전',
  `from_node_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '출발 노드',
  `to_node_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '도착 노드',
  `status` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'ACTIVE' COMMENT '이동 표시 상태',
  `travel_mode` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'WALK' COMMENT '이동수단',
  `departure_at` datetime(3) DEFAULT NULL COMMENT '출발 시각(UTC)',
  `arrival_at` datetime(3) DEFAULT NULL COMMENT '도착 시각(UTC)',
  `duration_min` int unsigned DEFAULT NULL COMMENT '이동시간(분)',
  `distance_m` int unsigned DEFAULT NULL COMMENT '이동거리(m)',
  `estimated_cost_krw` int unsigned DEFAULT NULL COMMENT '전체 인원 이동 비용(원)',
  `route_snapshot` json DEFAULT NULL COMMENT '경로·환승·출처·조회 시각',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_edge_nodes` (`version_id`,`from_node_id`,`to_node_id`),
  KEY `ix_edge_display` (`version_id`,`status`),
  KEY `fk_edge_to` (`version_id`,`to_node_id`),
  CONSTRAINT `fk_edge_from` FOREIGN KEY (`version_id`, `from_node_id`) REFERENCES `itinerary_nodes` (`version_id`, `id`),
  CONSTRAINT `fk_edge_to` FOREIGN KEY (`version_id`, `to_node_id`) REFERENCES `itinerary_nodes` (`version_id`, `id`),
  CONSTRAINT `ck_edge_distinct` CHECK ((`from_node_id` <> `to_node_id`)),
  CONSTRAINT `ck_edge_times` CHECK (((`departure_at` is null) or (`arrival_at` is null) or (`arrival_at` >= `departure_at`))),
  CONSTRAINT `ck_itinerary_edges_route_snapshot` CHECK (((`route_snapshot` is null) or (json_type(`route_snapshot`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_itinerary_edges_schema_version` CHECK ((`schema_version` > 0)),
  CONSTRAINT `ck_itinerary_edges_status` CHECK ((`status` in (_utf8mb4'ACTIVE',_utf8mb4'REMOVED'))),
  CONSTRAINT `ck_itinerary_edges_travel_mode` CHECK ((`travel_mode` in (_utf8mb4'CAR',_utf8mb4'PUBLIC_TRANSIT',_utf8mb4'WALK')))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='로드맵 이동 구간';
```

### itinerary_nodes

```sql
CREATE TABLE `itinerary_nodes` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `version_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '소속 초안·버전',
  `node_key` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '버전 간 같은 노드의 식별자',
  `day_no` smallint unsigned NOT NULL COMMENT '여행 일차',
  `order_index` int unsigned NOT NULL COMMENT '방문 순서',
  `node_kind` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'PLACE' COMMENT '노드 종류',
  `status` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'ACTIVE' COMMENT '노드 표시 상태',
  `place_id` char(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '연결된 원천 장소',
  `title` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '화면 표시 이름',
  `start_at` datetime(3) DEFAULT NULL COMMENT '방문 시작 시각(UTC)',
  `end_at` datetime(3) DEFAULT NULL COMMENT '방문 종료 시각(UTC)',
  `estimated_cost_krw` int unsigned DEFAULT NULL COMMENT '해당 방문의 전체 인원 비용(원)',
  `is_locked` tinyint(1) NOT NULL DEFAULT '0' COMMENT '고정 일정 여부',
  `selection_reason` text COLLATE utf8mb4_unicode_ci COMMENT '선정 이유',
  `place_snapshot` json DEFAULT NULL COMMENT '당시 장소·좌표·운영시간·가격·출처',
  `active_order_index` int unsigned GENERATED ALWAYS AS ((case when (`status` = _utf8mb4'ACTIVE') then `order_index` else NULL end)) STORED COMMENT '활성 노드 방문 순서 보장용 계산 컬럼',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_node_version_id` (`version_id`,`id`),
  UNIQUE KEY `uq_node_key` (`version_id`,`node_key`),
  UNIQUE KEY `uq_active_node_order` (`version_id`,`day_no`,`active_order_index`),
  KEY `ix_node_place` (`place_id`),
  KEY `ix_node_display` (`version_id`,`status`,`day_no`,`order_index`),
  CONSTRAINT `fk_node_place` FOREIGN KEY (`place_id`) REFERENCES `places` (`id`),
  CONSTRAINT `fk_node_version` FOREIGN KEY (`version_id`) REFERENCES `plan_versions` (`id`),
  CONSTRAINT `ck_itinerary_nodes_is_locked` CHECK (((`is_locked` is null) or (`is_locked` in (0,1)))),
  CONSTRAINT `ck_itinerary_nodes_node_kind` CHECK ((`node_kind` in (_utf8mb4'START',_utf8mb4'PLACE',_utf8mb4'FIXED_EVENT',_utf8mb4'END'))),
  CONSTRAINT `ck_itinerary_nodes_place_snapshot` CHECK (((`place_snapshot` is null) or (json_type(`place_snapshot`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_itinerary_nodes_schema_version` CHECK ((`schema_version` > 0)),
  CONSTRAINT `ck_itinerary_nodes_status` CHECK ((`status` in (_utf8mb4'ACTIVE',_utf8mb4'REMOVED'))),
  CONSTRAINT `ck_node_order` CHECK (((`day_no` > 0) and (`order_index` > 0))),
  CONSTRAINT `ck_node_place_required` CHECK (((`node_kind` <> _utf8mb4'PLACE') or (`place_id` is not null))),
  CONSTRAINT `ck_node_times` CHECK (((`start_at` is null) or (`end_at` is null) or (`end_at` >= `start_at`)))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='로드맵 노드';
```

### lodging_details

```sql
CREATE TABLE `lodging_details` (
  `place_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '숙소 장소 ID',
  `check_in_time` time DEFAULT NULL COMMENT '일반 체크인 시각(현지)',
  `check_out_time` time DEFAULT NULL COMMENT '일반 체크아웃 시각(현지)',
  `estimated_nightly_krw` int unsigned DEFAULT NULL COMMENT '참고 1박 요금(원)',
  `price_basis_people` smallint unsigned DEFAULT NULL COMMENT '참고 요금 기준 인원',
  `max_guests_per_room` smallint unsigned DEFAULT NULL COMMENT '확인된 객실 최대 인원',
  `room_info` json DEFAULT NULL COMMENT '객실별 조건·요금 근거',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`place_id`),
  CONSTRAINT `fk_lodging_place` FOREIGN KEY (`place_id`) REFERENCES `places` (`id`),
  CONSTRAINT `ck_lodging_basis` CHECK (((`price_basis_people` is null) or (`price_basis_people` > 0))),
  CONSTRAINT `ck_lodging_details_room_info` CHECK (((`room_info` is null) or (json_type(`room_info`) = _utf8mb4'ARRAY'))),
  CONSTRAINT `ck_lodging_details_schema_version` CHECK ((`schema_version` > 0)),
  CONSTRAINT `ck_lodging_guests` CHECK (((`max_guests_per_room` is null) or (`max_guests_per_room` > 0)))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='숙소 상세';
```

### member_profiles

```sql
CREATE TABLE `member_profiles` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '여행방 ID',
  `member_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '작성 참여자',
  `raw_input` text COLLATE utf8mb4_unicode_ci COMMENT '사용자 입력 원문',
  `parsed_result` json DEFAULT NULL COMMENT 'AI 파싱 결과 ParsedProfile',
  `confirmed_result` json DEFAULT NULL COMMENT '사용자가 확인한 최종 ParsedProfile',
  `revision` int unsigned NOT NULL DEFAULT '1' COMMENT '개인 조건 수정 번호',
  `confirmed_revision` int unsigned DEFAULT NULL COMMENT '확인한 개인 조건 버전',
  `confirmed_condition_revision` int unsigned DEFAULT NULL COMMENT '확인 당시 공통 조건 버전',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_profile_member` (`member_id`),
  UNIQUE KEY `uq_profile_trip_id` (`trip_id`,`id`),
  KEY `fk_profile_member` (`trip_id`,`member_id`),
  CONSTRAINT `fk_profile_member` FOREIGN KEY (`trip_id`, `member_id`) REFERENCES `trip_members` (`trip_id`, `id`),
  CONSTRAINT `ck_member_profiles_confirmed_result` CHECK (((`confirmed_result` is null) or (json_type(`confirmed_result`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_member_profiles_parsed_result` CHECK (((`parsed_result` is null) or (json_type(`parsed_result`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_member_profiles_schema_version` CHECK ((`schema_version` > 0)),
  CONSTRAINT `ck_profile_confirmation` CHECK (((`confirmed_revision` is null) = (`confirmed_condition_revision` is null))),
  CONSTRAINT `ck_profile_revision` CHECK (((`revision` > 0) and ((`confirmed_revision` is null) or (`confirmed_revision` <= `revision`))))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='개인 입력·파싱·확정';
```

### member_questions

```sql
CREATE TABLE `member_questions` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '여행방 ID',
  `profile_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '대상 개인 입력',
  `profile_revision` int unsigned NOT NULL COMMENT '질문 생성 당시 입력 버전',
  `question` text COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '질문',
  `answer` text COLLATE utf8mb4_unicode_ci COMMENT '답변',
  `status` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'OPEN' COMMENT '질문 상태',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  KEY `ix_question_profile` (`profile_id`,`profile_revision`,`status`),
  KEY `fk_question_profile` (`trip_id`,`profile_id`),
  CONSTRAINT `fk_question_profile` FOREIGN KEY (`trip_id`, `profile_id`) REFERENCES `member_profiles` (`trip_id`, `id`),
  CONSTRAINT `ck_member_questions_status` CHECK ((`status` in (_utf8mb4'OPEN',_utf8mb4'ANSWERED',_utf8mb4'SKIPPED',_utf8mb4'STALE'))),
  CONSTRAINT `ck_question_revision` CHECK ((`profile_revision` > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='개인 추가 질문·답변';
```

### place_candidates

```sql
CREATE TABLE `place_candidates` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `request_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '추천 AI 요청',
  `version_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '대상 초안',
  `target_node_id` char(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '기준 노드',
  `place_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '후보 장소',
  `operation` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'REPLACE' COMMENT '후보 적용 방식',
  `rank_no` int unsigned NOT NULL COMMENT '추천 순위',
  `score` decimal(10,3) DEFAULT NULL COMMENT '추천 점수',
  `reason` text COLLATE utf8mb4_unicode_ci COMMENT '추천 이유',
  `estimated_changes` json DEFAULT NULL COMMENT '시간·비용·제약 변화',
  `status` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'PROPOSED' COMMENT '후보 상태',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_candidate_rank` (`request_id`,`rank_no`),
  UNIQUE KEY `uq_candidate_place` (`request_id`,`place_id`),
  KEY `ix_candidate_target` (`version_id`,`target_node_id`,`status`),
  KEY `fk_candidate_request` (`version_id`,`request_id`),
  KEY `fk_candidate_place` (`place_id`),
  CONSTRAINT `fk_candidate_place` FOREIGN KEY (`place_id`) REFERENCES `places` (`id`),
  CONSTRAINT `fk_candidate_request` FOREIGN KEY (`version_id`, `request_id`) REFERENCES `ai_requests` (`version_id`, `id`),
  CONSTRAINT `fk_candidate_target` FOREIGN KEY (`version_id`, `target_node_id`) REFERENCES `itinerary_nodes` (`version_id`, `id`),
  CONSTRAINT `ck_candidate_rank` CHECK ((`rank_no` > 0)),
  CONSTRAINT `ck_candidate_target` CHECK (((`operation` = _utf8mb4'GENERATE') or (`target_node_id` is not null))),
  CONSTRAINT `ck_place_candidates_estimated_changes` CHECK (((`estimated_changes` is null) or (json_type(`estimated_changes`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_place_candidates_operation` CHECK ((`operation` in (_utf8mb4'GENERATE',_utf8mb4'REPLACE',_utf8mb4'INSERT_BEFORE',_utf8mb4'INSERT_AFTER'))),
  CONSTRAINT `ck_place_candidates_schema_version` CHECK ((`schema_version` > 0)),
  CONSTRAINT `ck_place_candidates_status` CHECK ((`status` in (_utf8mb4'PROPOSED',_utf8mb4'SELECTED',_utf8mb4'DISMISSED',_utf8mb4'STALE')))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='교체·추가 장소 후보';
```

### place_sources

```sql
CREATE TABLE `place_sources` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `place_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '대표 장소 ID',
  `provider` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '제공처',
  `external_id` varchar(160) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL COMMENT '제공처의 장소 식별자',
  `source_url` text COLLATE utf8mb4_unicode_ci COMMENT '원본 URL',
  `raw_data` json DEFAULT NULL COMMENT '수집 원본',
  `fetched_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '수집 시각(UTC)',
  `source_updated_at` datetime(3) DEFAULT NULL COMMENT '원본 갱신 시각(UTC)',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_source_external` (`provider`,`external_id`),
  KEY `ix_source_place` (`place_id`),
  KEY `ix_source_refresh` (`provider`,`fetched_at`),
  CONSTRAINT `fk_source_place` FOREIGN KEY (`place_id`) REFERENCES `places` (`id`),
  CONSTRAINT `ck_place_sources_raw_data` CHECK (((`raw_data` is null) or (json_type(`raw_data`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_place_sources_schema_version` CHECK ((`schema_version` > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='장소 원본·출처';
```

### place_tag_links

```sql
CREATE TABLE `place_tag_links` (
  `place_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '장소 ID',
  `tag_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '태그 ID',
  `source_type` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'CURATED' COMMENT '태그 판단 출처',
  `evidence` text COLLATE utf8mb4_unicode_ci COMMENT '태그 판단 근거',
  `confidence` decimal(4,3) DEFAULT NULL COMMENT 'AI 추정 신뢰도, 없으면 NULL',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`place_id`,`tag_id`),
  KEY `ix_tag_place` (`tag_id`,`place_id`),
  CONSTRAINT `fk_taglink_place` FOREIGN KEY (`place_id`) REFERENCES `places` (`id`),
  CONSTRAINT `fk_taglink_tag` FOREIGN KEY (`tag_id`) REFERENCES `place_tags` (`id`),
  CONSTRAINT `ck_place_tag_links_source_type` CHECK ((`source_type` in (_utf8mb4'PROVIDER',_utf8mb4'CURATED',_utf8mb4'AI_INFERRED'))),
  CONSTRAINT `ck_tag_confidence` CHECK (((`confidence` is null) or (`confidence` between 0 and 1)))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='장소별 추천 태그';
```

### place_tags

```sql
CREATE TABLE `place_tags` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `code` varchar(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '공통 태그 코드',
  `name` varchar(80) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '태그명',
  `category` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '취향·환경·시설 등',
  `is_active` tinyint(1) NOT NULL DEFAULT '1' COMMENT '사용 여부',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_tag_code` (`code`),
  CONSTRAINT `ck_place_tags_is_active` CHECK (((`is_active` is null) or (`is_active` in (0,1))))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='추천 태그 사전';
```

### places

```sql
CREATE TABLE `places` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `region_code` varchar(20) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '소속 지역',
  `name` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '대표 장소명',
  `category` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'ATTRACTION' COMMENT '주 분류',
  `address` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '주소',
  `latitude` decimal(10,7) DEFAULT NULL COMMENT '위도',
  `longitude` decimal(10,7) DEFAULT NULL COMMENT '경도',
  `phone` varchar(80) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '연락처',
  `operating_status` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'UNKNOWN' COMMENT '운영 상태',
  `stay_min` smallint unsigned DEFAULT NULL COMMENT '예상 체류시간(분)',
  `parking_available` tinyint(1) DEFAULT NULL COMMENT '주차 가능, 미확인은 NULL',
  `reservation_required` tinyint(1) DEFAULT NULL COMMENT '예약 필요, 미확인은 NULL',
  `accessibility_info` json DEFAULT NULL COMMENT '경사·계단·접근성 정보와 근거',
  `opening_hours` json DEFAULT NULL COMMENT '요일별 운영시간과 휴무',
  `price_info` json DEFAULT NULL COMMENT '가격과 적용 기준',
  `photo_refs` json DEFAULT NULL COMMENT '사진 URL·제공처·출처',
  `review_refs` json DEFAULT NULL COMMENT '리뷰 제공처·참조 URL',
  `verified_at` datetime(3) DEFAULT NULL COMMENT '정보 확인 시각(UTC)',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  KEY `ix_place_filter` (`region_code`,`category`,`operating_status`),
  KEY `ix_place_coords` (`latitude`,`longitude`),
  KEY `ix_place_name` (`name`),
  KEY `ix_place_verified` (`verified_at`),
  CONSTRAINT `fk_place_region` FOREIGN KEY (`region_code`) REFERENCES `regions` (`code`),
  CONSTRAINT `ck_place_coords` CHECK (((`latitude` is null) = (`longitude` is null))),
  CONSTRAINT `ck_place_lat` CHECK (((`latitude` is null) or (`latitude` between -(90) and 90))),
  CONSTRAINT `ck_place_lon` CHECK (((`longitude` is null) or (`longitude` between -(180) and 180))),
  CONSTRAINT `ck_places_accessibility_info` CHECK (((`accessibility_info` is null) or (json_type(`accessibility_info`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_places_category` CHECK ((`category` in (_utf8mb4'ATTRACTION',_utf8mb4'RESTAURANT',_utf8mb4'CAFE',_utf8mb4'LODGING'))),
  CONSTRAINT `ck_places_opening_hours` CHECK (((`opening_hours` is null) or (json_type(`opening_hours`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_places_operating_status` CHECK ((`operating_status` in (_utf8mb4'OPEN',_utf8mb4'TEMP_CLOSED',_utf8mb4'CLOSED',_utf8mb4'UNKNOWN'))),
  CONSTRAINT `ck_places_parking_available` CHECK (((`parking_available` is null) or (`parking_available` in (0,1)))),
  CONSTRAINT `ck_places_photo_refs` CHECK (((`photo_refs` is null) or (json_type(`photo_refs`) = _utf8mb4'ARRAY'))),
  CONSTRAINT `ck_places_price_info` CHECK (((`price_info` is null) or (json_type(`price_info`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_places_reservation_required` CHECK (((`reservation_required` is null) or (`reservation_required` in (0,1)))),
  CONSTRAINT `ck_places_review_refs` CHECK (((`review_refs` is null) or (json_type(`review_refs`) = _utf8mb4'ARRAY'))),
  CONSTRAINT `ck_places_schema_version` CHECK ((`schema_version` > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='충북 전체 장소';
```

### plan_versions

```sql
CREATE TABLE `plan_versions` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '여행방 ID',
  `plan_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '여행 계획 ID',
  `group_snapshot_id` char(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '생성 기준 단체 조건',
  `base_version_id` char(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '편집 시작 당시 버전',
  `restored_from_version_id` char(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '복원 원본 버전',
  `version_no` int unsigned DEFAULT NULL COMMENT '저장 버전 번호, 초안은 NULL',
  `status` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'DRAFT' COMMENT '버전 상태',
  `origin` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'GENERATED' COMMENT '생성 경위',
  `edit_revision` int unsigned NOT NULL DEFAULT '1' COMMENT '초안 수정 번호',
  `rules_snapshot` json DEFAULT NULL COMMENT '사용한 추천 규칙 사본',
  `excluded_place_ids` json DEFAULT NULL COMMENT '당시 제외 장소 ID 배열',
  `validation_result` json DEFAULT NULL COMMENT 'ValidationResult 검사 결과',
  `summary` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '변경 요약',
  `created_by` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '작성 참여자',
  `saved_at` datetime(3) DEFAULT NULL COMMENT '저장 확정 시각(UTC)',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_version_trip_id` (`trip_id`,`id`),
  UNIQUE KEY `uq_version_plan_id` (`plan_id`,`id`),
  UNIQUE KEY `uq_version_number` (`plan_id`,`version_no`),
  KEY `ix_version_status` (`plan_id`,`status`,`updated_at`),
  KEY `fk_version_plan` (`trip_id`,`plan_id`),
  KEY `fk_version_snapshot` (`trip_id`,`group_snapshot_id`),
  KEY `fk_version_base` (`plan_id`,`base_version_id`),
  KEY `fk_version_restore` (`plan_id`,`restored_from_version_id`),
  KEY `fk_version_member` (`trip_id`,`created_by`),
  CONSTRAINT `fk_version_base` FOREIGN KEY (`plan_id`, `base_version_id`) REFERENCES `plan_versions` (`plan_id`, `id`),
  CONSTRAINT `fk_version_member` FOREIGN KEY (`trip_id`, `created_by`) REFERENCES `trip_members` (`trip_id`, `id`),
  CONSTRAINT `fk_version_plan` FOREIGN KEY (`trip_id`, `plan_id`) REFERENCES `plans` (`trip_id`, `id`),
  CONSTRAINT `fk_version_restore` FOREIGN KEY (`plan_id`, `restored_from_version_id`) REFERENCES `plan_versions` (`plan_id`, `id`),
  CONSTRAINT `fk_version_snapshot` FOREIGN KEY (`trip_id`, `group_snapshot_id`) REFERENCES `group_snapshots` (`trip_id`, `id`),
  CONSTRAINT `ck_plan_versions_excluded_place_ids` CHECK (((`excluded_place_ids` is null) or (json_type(`excluded_place_ids`) = _utf8mb4'ARRAY'))),
  CONSTRAINT `ck_plan_versions_origin` CHECK ((`origin` in (_utf8mb4'GENERATED',_utf8mb4'MANUAL_EDIT',_utf8mb4'CHAT_EDIT',_utf8mb4'RESTORE'))),
  CONSTRAINT `ck_plan_versions_rules_snapshot` CHECK (((`rules_snapshot` is null) or (json_type(`rules_snapshot`) = _utf8mb4'ARRAY'))),
  CONSTRAINT `ck_plan_versions_schema_version` CHECK ((`schema_version` > 0)),
  CONSTRAINT `ck_plan_versions_status` CHECK ((`status` in (_utf8mb4'DRAFT',_utf8mb4'SAVED',_utf8mb4'ABANDONED'))),
  CONSTRAINT `ck_plan_versions_validation_result` CHECK (((`validation_result` is null) or (json_type(`validation_result`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_version_not_self` CHECK ((((`base_version_id` is null) or (`base_version_id` <> `id`)) and ((`restored_from_version_id` is null) or (`restored_from_version_id` <> `id`)))),
  CONSTRAINT `ck_version_revision` CHECK ((`edit_revision` > 0)),
  CONSTRAINT `ck_version_saved` CHECK ((((`status` = _utf8mb4'SAVED') and (`version_no` is not null) and (`version_no` > 0) and (`saved_at` is not null)) or ((`status` <> _utf8mb4'SAVED') and (`version_no` is null) and (`saved_at` is null))))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='편집 초안·저장 버전';
```

### plans

```sql
CREATE TABLE `plans` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '여행방 ID',
  `name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '계획 이름',
  `current_version_id` char(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '현재 저장 버전',
  `active_draft_id` char(36) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '공유 편집 초안',
  `lock_version` int unsigned NOT NULL DEFAULT '1' COMMENT '현재 버전 변경 번호',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_plan_trip_id` (`trip_id`,`id`),
  KEY `ix_plan_trip` (`trip_id`,`updated_at`),
  KEY `fk_plan_current` (`id`,`current_version_id`),
  KEY `fk_plan_draft` (`id`,`active_draft_id`),
  CONSTRAINT `fk_plan_current` FOREIGN KEY (`id`, `current_version_id`) REFERENCES `plan_versions` (`plan_id`, `id`),
  CONSTRAINT `fk_plan_draft` FOREIGN KEY (`id`, `active_draft_id`) REFERENCES `plan_versions` (`plan_id`, `id`),
  CONSTRAINT `fk_plan_trip` FOREIGN KEY (`trip_id`) REFERENCES `trips` (`id`),
  CONSTRAINT `ck_plan_lock` CHECK ((`lock_version` > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='여행 계획과 현재 버전';
```

### recommendation_rules

```sql
CREATE TABLE `recommendation_rules` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `rule_key` varchar(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '규칙 코드',
  `version` int unsigned NOT NULL DEFAULT '1' COMMENT '규칙 버전',
  `rule_type` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'SCORE' COMMENT '필수 제외 또는 점수 조정',
  `condition_json` json NOT NULL COMMENT '허용된 규칙 형식의 적용 조건',
  `effect_json` json NOT NULL COMMENT '제외·가점·감점 효과',
  `weight` decimal(10,3) NOT NULL DEFAULT '1.000' COMMENT '가중치',
  `is_active` tinyint(1) NOT NULL DEFAULT '1' COMMENT '사용 여부',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rule_version` (`rule_key`,`version`),
  KEY `ix_rule_active` (`is_active`,`rule_type`),
  CONSTRAINT `ck_recommendation_rules_condition_json` CHECK (((`condition_json` is null) or (json_type(`condition_json`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_recommendation_rules_effect_json` CHECK (((`effect_json` is null) or (json_type(`effect_json`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_recommendation_rules_is_active` CHECK (((`is_active` is null) or (`is_active` in (0,1)))),
  CONSTRAINT `ck_recommendation_rules_rule_type` CHECK ((`rule_type` in (_utf8mb4'HARD_FILTER',_utf8mb4'SCORE'))),
  CONSTRAINT `ck_recommendation_rules_schema_version` CHECK ((`schema_version` > 0)),
  CONSTRAINT `ck_rule_version` CHECK ((`version` > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='추천·제외 규칙';
```

### regions

```sql
CREATE TABLE `regions` (
  `code` varchar(20) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '서비스 내부 지역 코드',
  `name` varchar(80) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '지역명',
  `parent_code` varchar(20) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '상위 지역 코드',
  `level_no` tinyint unsigned NOT NULL DEFAULT '1' COMMENT '지역 단계',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`code`),
  KEY `ix_region_parent` (`parent_code`),
  CONSTRAINT `fk_region_parent` FOREIGN KEY (`parent_code`) REFERENCES `regions` (`code`),
  CONSTRAINT `ck_region_level` CHECK ((`level_no` between 1 and 4))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='지역';
```

### restaurant_details

```sql
CREATE TABLE `restaurant_details` (
  `place_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '음식점·카페 장소 ID',
  `cuisine_type` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '음식 종류',
  `menu_info` json DEFAULT NULL COMMENT '메뉴명·가격·적용 기준',
  `avg_price_krw` int unsigned DEFAULT NULL COMMENT '예상 1인 비용(원)',
  `dietary_info` json DEFAULT NULL COMMENT '확인된 음식 제한 관련 정보와 근거',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`place_id`),
  CONSTRAINT `fk_restaurant_place` FOREIGN KEY (`place_id`) REFERENCES `places` (`id`),
  CONSTRAINT `ck_restaurant_details_dietary_info` CHECK (((`dietary_info` is null) or (json_type(`dietary_info`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_restaurant_details_menu_info` CHECK (((`menu_info` is null) or (json_type(`menu_info`) = _utf8mb4'ARRAY'))),
  CONSTRAINT `ck_restaurant_details_schema_version` CHECK ((`schema_version` > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='음식점·카페 상세';
```

### tourism_details

```sql
CREATE TABLE `tourism_details` (
  `place_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '관광지 장소 ID',
  `tourism_type` varchar(80) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '자연·문화·전시·체험 등',
  `admission_fee_krw` int unsigned DEFAULT NULL COMMENT '대표 입장료(1인, 원)',
  `estimated_walk_min` smallint unsigned DEFAULT NULL COMMENT '예상 총 도보시간(분)',
  `max_walk_segment_min` smallint unsigned DEFAULT NULL COMMENT '최장 연속 도보시간(분)',
  `facilities` json DEFAULT NULL COMMENT '편의시설과 확인 근거',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`place_id`),
  CONSTRAINT `fk_tourism_place` FOREIGN KEY (`place_id`) REFERENCES `places` (`id`),
  CONSTRAINT `ck_tourism_details_facilities` CHECK (((`facilities` is null) or (json_type(`facilities`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_tourism_details_schema_version` CHECK ((`schema_version` > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='관광지 상세';
```

### trip_conditions

```sql
CREATE TABLE `trip_conditions` (
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '여행방 ID',
  `region_codes` json DEFAULT NULL COMMENT '선택한 지역 코드 배열',
  `start_date` date DEFAULT NULL COMMENT '여행 시작일',
  `end_date` date DEFAULT NULL COMMENT '여행 종료일',
  `start_point` json DEFAULT NULL COMMENT '출발지 PlaceRef',
  `end_point` json DEFAULT NULL COMMENT '종료지 PlaceRef',
  `party_size` smallint unsigned DEFAULT NULL COMMENT '전체 여행 인원',
  `travel_mode` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'CAR 또는 PUBLIC_TRANSIT, 미정은 NULL',
  `budget_total_krw` int unsigned DEFAULT NULL COMMENT '전체 인원 총예산(원)',
  `lodging_required` tinyint(1) DEFAULT NULL COMMENT '숙박 필요 여부',
  `fixed_events` json DEFAULT NULL COMMENT '고정 일정 배열',
  `revision` int unsigned NOT NULL DEFAULT '1' COMMENT '공통 조건 수정 번호',
  `schema_version` smallint unsigned NOT NULL DEFAULT '1' COMMENT 'JSON 구조 버전',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`trip_id`),
  CONSTRAINT `fk_condition_trip` FOREIGN KEY (`trip_id`) REFERENCES `trips` (`id`),
  CONSTRAINT `ck_condition_dates` CHECK (((`start_date` is null) or (`end_date` is null) or (`end_date` >= `start_date`))),
  CONSTRAINT `ck_condition_mode` CHECK (((`travel_mode` is null) or (`travel_mode` in (_utf8mb4'CAR',_utf8mb4'PUBLIC_TRANSIT')))),
  CONSTRAINT `ck_condition_people` CHECK (((`party_size` is null) or (`party_size` > 0))),
  CONSTRAINT `ck_condition_revision` CHECK ((`revision` > 0)),
  CONSTRAINT `ck_trip_conditions_end_point` CHECK (((`end_point` is null) or (json_type(`end_point`) = _utf8mb4'OBJECT'))),
  CONSTRAINT `ck_trip_conditions_fixed_events` CHECK (((`fixed_events` is null) or (json_type(`fixed_events`) = _utf8mb4'ARRAY'))),
  CONSTRAINT `ck_trip_conditions_lodging_required` CHECK (((`lodging_required` is null) or (`lodging_required` in (0,1)))),
  CONSTRAINT `ck_trip_conditions_region_codes` CHECK (((`region_codes` is null) or (json_type(`region_codes`) = _utf8mb4'ARRAY'))),
  CONSTRAINT `ck_trip_conditions_schema_version` CHECK ((`schema_version` > 0)),
  CONSTRAINT `ck_trip_conditions_start_point` CHECK (((`start_point` is null) or (json_type(`start_point`) = _utf8mb4'OBJECT')))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='공통 여행 조건';
```

### trip_excluded_places

```sql
CREATE TABLE `trip_excluded_places` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '여행방',
  `place_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '제외 장소',
  `excluded_by` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '제외 참여자',
  `reason` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '제외 이유',
  `is_active` tinyint(1) NOT NULL DEFAULT '1' COMMENT '현재 제외 여부',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_excluded_place` (`trip_id`,`place_id`),
  KEY `ix_excluded_active` (`trip_id`,`is_active`),
  KEY `fk_excluded_place` (`place_id`),
  KEY `fk_excluded_member` (`trip_id`,`excluded_by`),
  CONSTRAINT `fk_excluded_member` FOREIGN KEY (`trip_id`, `excluded_by`) REFERENCES `trip_members` (`trip_id`, `id`),
  CONSTRAINT `fk_excluded_place` FOREIGN KEY (`place_id`) REFERENCES `places` (`id`),
  CONSTRAINT `fk_excluded_trip` FOREIGN KEY (`trip_id`) REFERENCES `trips` (`id`),
  CONSTRAINT `ck_trip_excluded_places_is_active` CHECK (((`is_active` is null) or (`is_active` in (0,1))))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='이번 여행의 제외 장소';
```

### trip_members

```sql
CREATE TABLE `trip_members` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '여행방 ID',
  `nickname` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '참여자 이름',
  `role` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'MEMBER' COMMENT '방장 또는 참여자',
  `status` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'ACTIVE' COMMENT '참여 상태',
  `reconnect_token_hash` varchar(255) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL COMMENT '재접속 토큰 해시',
  `active_owner_trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin GENERATED ALWAYS AS ((case when ((`role` = _utf8mb4'OWNER') and (`status` = _utf8mb4'ACTIVE')) then `trip_id` else NULL end)) STORED COMMENT '활성 방장 1명 보장용 계산 컬럼',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_member_trip_id` (`trip_id`,`id`),
  UNIQUE KEY `uq_member_token` (`reconnect_token_hash`),
  UNIQUE KEY `uq_active_owner` (`active_owner_trip_id`),
  KEY `ix_member_status` (`trip_id`,`status`),
  CONSTRAINT `fk_member_trip` FOREIGN KEY (`trip_id`) REFERENCES `trips` (`id`),
  CONSTRAINT `ck_trip_members_role` CHECK ((`role` in (_utf8mb4'OWNER',_utf8mb4'MEMBER'))),
  CONSTRAINT `ck_trip_members_status` CHECK ((`status` in (_utf8mb4'ACTIVE',_utf8mb4'LEFT')))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='여행방 참여자';
```

### trips

```sql
CREATE TABLE `trips` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '고유 식별자',
  `name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '방 이름',
  `invite_code` varchar(20) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '초대 코드',
  `status` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'OPEN' COMMENT '방 상태',
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '생성 시각(UTC)',
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '수정 시각(UTC)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_trip_invite` (`invite_code`),
  CONSTRAINT `ck_trips_status` CHECK ((`status` in (_utf8mb4'OPEN',_utf8mb4'CLOSED')))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='여행방';
```
