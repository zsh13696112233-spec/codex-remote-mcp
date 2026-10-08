ALTER TABLE codex_sop_task_definitions ADD COLUMN blocked_notification_group_id VARCHAR(36);
CREATE TABLE codex_blocked_notification_settings (
 id INTEGER PRIMARY KEY,
 template_id VARCHAR(256) NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
INSERT INTO codex_blocked_notification_settings(id, template_id) VALUES (1, '');
CREATE TABLE codex_jira_developer_mappings (
 target_id VARCHAR(36) PRIMARY KEY,
 identity_hash VARCHAR(64) NOT NULL UNIQUE,
 account_type VARCHAR(16) NOT NULL,
 account_id VARCHAR(256) NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE TABLE codex_blocked_notifications (
 id VARCHAR(128) PRIMARY KEY,
 workflow_id VARCHAR(128) UNIQUE,
 group_json TEXT NOT NULL,
 person_json TEXT,
 payload_json TEXT,
 state VARCHAR(24) NOT NULL,
 reason VARCHAR(1000) NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,
 next_at TIMESTAMP(6) NOT NULL,
 updated_at TIMESTAMP(6) NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_blocked_notification_due ON codex_blocked_notifications(state, next_at);
