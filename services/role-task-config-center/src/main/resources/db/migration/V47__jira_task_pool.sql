CREATE TABLE codex_jira_requests (
  id VARCHAR(36) PRIMARY KEY,
  task_definition_id VARCHAR(36) NOT NULL,
  source VARCHAR(16) NOT NULL,
  spec_json LONGTEXT NOT NULL,
  schedule_json LONGTEXT,
  state VARCHAR(24) NOT NULL,
  reason VARCHAR(1000) NOT NULL DEFAULT '',
  check_state VARCHAR(16) NOT NULL DEFAULT 'pending',
  reservation_id VARCHAR(36),
  workflow_id VARCHAR(128),
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
);
CREATE INDEX idx_jira_requests_pending ON codex_jira_requests(state, created_at);
CREATE TABLE codex_jira_dingtalk_requests (
  client_id VARCHAR(128) NOT NULL,
  message_id VARCHAR(256) NOT NULL,
  request_id VARCHAR(36) NOT NULL,
  message_json LONGTEXT NOT NULL,
  finished BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY(client_id,message_id)
);
