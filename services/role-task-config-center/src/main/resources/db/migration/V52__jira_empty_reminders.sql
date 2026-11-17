CREATE TABLE codex_jira_empty_reminders (
  account_id VARCHAR(36) NOT NULL PRIMARY KEY,
  version BIGINT NOT NULL,
  enabled BOOLEAN NOT NULL,
  config_json LONGTEXT NOT NULL,
  state_json LONGTEXT NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE TABLE codex_jira_empty_reminder_deliveries (
  id VARCHAR(36) NOT NULL PRIMARY KEY,
  account_id VARCHAR(36) NOT NULL,
  created_at TIMESTAMP(6) NOT NULL,
  status VARCHAR(32) NOT NULL,
  INDEX idx_jira_empty_delivery_account (account_id, created_at)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
