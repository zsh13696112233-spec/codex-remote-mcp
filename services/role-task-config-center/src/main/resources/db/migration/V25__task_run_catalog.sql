ALTER TABLE codex_sop_task_runs ADD COLUMN trigger_source VARCHAR(16) NOT NULL DEFAULT 'unknown';
ALTER TABLE codex_sop_task_runs ADD COLUMN run_name VARCHAR(255) NOT NULL DEFAULT '';
CREATE INDEX idx_runs_catalog_source ON codex_sop_task_runs(trigger_source, submitted_at, workflow_id);
CREATE INDEX idx_runs_catalog_time ON codex_sop_task_runs(submitted_at, workflow_id);
