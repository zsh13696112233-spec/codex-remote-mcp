ALTER TABLE codex_sop_sops ADD COLUMN jira_connection_id VARCHAR(36);
CREATE INDEX idx_sop_jira_connection ON codex_sop_sops(jira_connection_id);
