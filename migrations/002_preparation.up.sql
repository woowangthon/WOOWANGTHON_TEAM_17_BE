CREATE TABLE `trip_preparation_documents` (
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `conditions_json` json NOT NULL,
  `conditions_version` int unsigned NOT NULL DEFAULT 1,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`trip_id`),
  CONSTRAINT `fk_trip_preparation_trip` FOREIGN KEY (`trip_id`) REFERENCES `trips` (`id`) ON DELETE CASCADE,
  CONSTRAINT `ck_trip_preparation_version` CHECK (`conditions_version` > 0),
  CONSTRAINT `ck_trip_preparation_object` CHECK (JSON_TYPE(`conditions_json`) = 'OBJECT')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='여행 준비 공통 조건 문서';

CREATE TABLE `member_preparation_documents` (
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `member_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `draft_json` json NOT NULL,
  `preference_version` int unsigned NOT NULL DEFAULT 1,
  `confirmed_conditions_version` int unsigned DEFAULT NULL,
  `confirmed_preference_version` int unsigned DEFAULT NULL,
  `confirmed_at` datetime(3) DEFAULT NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`trip_id`,`member_id`),
  KEY `ix_member_preparation_member` (`member_id`),
  CONSTRAINT `fk_member_preparation_member` FOREIGN KEY (`trip_id`,`member_id`) REFERENCES `trip_members` (`trip_id`,`id`) ON DELETE CASCADE,
  CONSTRAINT `ck_member_preparation_version` CHECK (`preference_version` > 0),
  CONSTRAINT `ck_member_preparation_draft` CHECK (JSON_TYPE(`draft_json`) = 'OBJECT'),
  CONSTRAINT `ck_member_preparation_confirmation` CHECK ((`confirmed_conditions_version` IS NULL AND `confirmed_preference_version` IS NULL AND `confirmed_at` IS NULL) OR (`confirmed_conditions_version` IS NOT NULL AND `confirmed_preference_version` IS NOT NULL AND `confirmed_at` IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='여행 회원 개인 준비 문서';

CREATE TABLE `preparation_interpret_requests` (
  `session_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `method` varchar(8) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'POST',
  `path` varchar(160) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `idempotency_key` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `member_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `conditions_version` int unsigned NOT NULL,
  `preference_version` int unsigned NOT NULL,
  `fingerprint` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `status` varchar(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `lease_token` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `lease_expires_at` datetime(3) NOT NULL,
  `result_json` json DEFAULT NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `expires_at` datetime(3) NOT NULL,
  PRIMARY KEY (`session_id`,`method`,`path`,`idempotency_key`),
  KEY `ix_preparation_interpret_expiry` (`expires_at`),
  KEY `ix_preparation_interpret_member` (`trip_id`,`member_id`),
  CONSTRAINT `fk_preparation_interpret_session` FOREIGN KEY (`session_id`) REFERENCES `device_sessions` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_preparation_interpret_member` FOREIGN KEY (`trip_id`,`member_id`) REFERENCES `trip_members` (`trip_id`,`id`) ON DELETE CASCADE,
  CONSTRAINT `ck_preparation_interpret_method` CHECK (`method` = 'POST'),
  CONSTRAINT `ck_preparation_interpret_status` CHECK (`status` IN ('RUNNING','COMPLETE')),
  CONSTRAINT `ck_preparation_interpret_result` CHECK ((`status` = 'RUNNING' AND `result_json` IS NULL) OR (`status` = 'COMPLETE' AND `result_json` IS NOT NULL)),
  CONSTRAINT `ck_preparation_interpret_expiry` CHECK (`expires_at` > `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='여행 준비 AI 해석 멱등 요청';
