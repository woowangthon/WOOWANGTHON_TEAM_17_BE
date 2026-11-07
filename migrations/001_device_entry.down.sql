-- Destructive rollback: drops API state created by migration 001_device_entry.
DROP TABLE `api_idempotency`;
DROP TABLE `device_session_memberships`;
DROP TABLE `device_sessions`;
