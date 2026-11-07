CREATE TABLE `device_sessions` (
  `id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `credential_hash` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `expires_at` datetime(3) NOT NULL,
  `revoked_at` datetime(3) DEFAULT NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_device_session_credential` (`credential_hash`),
  KEY `ix_device_session_expiry` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='기기별 서버 발급 세션';

CREATE TABLE `device_session_memberships` (
  `session_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `member_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `last_accessed_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`session_id`,`trip_id`),
  KEY `ix_device_membership_trip_member` (`trip_id`,`member_id`),
  KEY `ix_device_membership_recent` (`session_id`,`last_accessed_at`,`trip_id`),
  CONSTRAINT `fk_device_membership_session` FOREIGN KEY (`session_id`) REFERENCES `device_sessions` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_device_membership_member` FOREIGN KEY (`trip_id`,`member_id`) REFERENCES `trip_members` (`trip_id`,`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='기기 세션의 여행 참여 자격';

CREATE TABLE `api_idempotency` (
  `session_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `method` varchar(8) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `path` varchar(160) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `idempotency_key` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `fingerprint` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `state` varchar(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `trip_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `member_id` char(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `http_status` smallint unsigned NOT NULL,
  `response_json` json NOT NULL,
  `created_at` datetime(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `expires_at` datetime(3) NOT NULL,
  PRIMARY KEY (`session_id`,`method`,`path`,`idempotency_key`),
  KEY `ix_api_idempotency_expiry` (`expires_at`),
  KEY `ix_api_idempotency_trip_member` (`trip_id`,`member_id`),
  CONSTRAINT `fk_api_idempotency_session` FOREIGN KEY (`session_id`) REFERENCES `device_sessions` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_api_idempotency_member` FOREIGN KEY (`trip_id`,`member_id`) REFERENCES `trip_members` (`trip_id`,`id`),
  CONSTRAINT `ck_api_idempotency_method` CHECK (`method` = 'POST'),
  CONSTRAINT `ck_api_idempotency_state` CHECK (`state` IN ('COMPLETE')),
  CONSTRAINT `ck_api_idempotency_status` CHECK (`http_status` IN (200,201)),
  CONSTRAINT `ck_api_idempotency_expiry` CHECK (`expires_at` > `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='여행방 시작 API의 내구성 있는 멱등 결과';
