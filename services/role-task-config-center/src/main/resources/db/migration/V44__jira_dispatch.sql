ALTER TABLE codex_sop_sops ADD COLUMN jira_jql TEXT NULL;
ALTER TABLE codex_sop_sops ADD COLUMN jira_account VARCHAR(128) NOT NULL DEFAULT '';
ALTER TABLE codex_task_schedules ADD COLUMN resource_pending BOOLEAN NOT NULL DEFAULT FALSE;
