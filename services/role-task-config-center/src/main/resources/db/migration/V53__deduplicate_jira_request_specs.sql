-- 相同派发配置只保存一次；历史请求继续读取原内联快照，不批量改写业务数据。
CREATE TABLE codex_jira_request_specs (
  id VARCHAR(64) PRIMARY KEY,
  spec_json LONGTEXT NOT NULL
) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

ALTER TABLE codex_jira_requests ADD COLUMN spec_snapshot_id VARCHAR(64);
ALTER TABLE codex_jira_requests ADD CONSTRAINT fk_jira_request_spec
  FOREIGN KEY (spec_snapshot_id) REFERENCES codex_jira_request_specs(id);
