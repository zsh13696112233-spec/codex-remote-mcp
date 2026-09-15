CREATE TABLE codex_task_schedules (
  id VARCHAR(36) PRIMARY KEY,
  name VARCHAR(160) NOT NULL,
  task_definition_id VARCHAR(36) NOT NULL UNIQUE,
  mode VARCHAR(16) NOT NULL,
  daily_time VARCHAR(5),
  interval_minutes INT,
  enabled BOOLEAN NOT NULL,
  next_at TIMESTAMP(6) NULL,
  dispatch_pending BOOLEAN NOT NULL DEFAULT FALSE,
  FOREIGN KEY (task_definition_id) REFERENCES codex_sop_task_definitions(id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE INDEX idx_schedule_due ON codex_task_schedules(enabled, next_at);
UPDATE codex_sop_task_definitions SET schedule_enabled = FALSE, next_interval_at = NULL;
