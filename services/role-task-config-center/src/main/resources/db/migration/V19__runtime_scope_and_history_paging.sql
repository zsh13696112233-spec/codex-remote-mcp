ALTER TABLE codex_sop_task_runs
  ADD COLUMN runtime_scope_registered BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX idx_task_runs_scope ON codex_sop_task_runs(task_definition_id, runtime_scope_registered, workflow_id);
CREATE INDEX idx_task_runs_scope_pending ON codex_sop_task_runs(runtime_scope_registered, task_definition_id);
CREATE INDEX idx_task_runs_history ON codex_sop_task_runs(task_definition_id, submitted_at, workflow_id);
