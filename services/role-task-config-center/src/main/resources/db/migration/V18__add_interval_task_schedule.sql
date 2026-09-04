ALTER TABLE codex_sop_task_definitions
  ADD COLUMN schedule_mode VARCHAR(16) NOT NULL DEFAULT 'daily';
ALTER TABLE codex_sop_task_definitions
  ADD COLUMN schedule_interval_minutes INT NULL;
ALTER TABLE codex_sop_task_definitions
  ADD COLUMN next_interval_at TIMESTAMP(6) NULL;

CREATE INDEX idx_task_interval_schedule
  ON codex_sop_task_definitions(
    schedule_enabled,
    schedule_mode,
    next_interval_at
  );
