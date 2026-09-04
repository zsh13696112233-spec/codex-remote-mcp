ALTER TABLE codex_sop_task_definitions
  ADD COLUMN schedule_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE codex_sop_task_definitions
  ADD COLUMN schedule_time TIME NULL;
ALTER TABLE codex_sop_task_definitions
  ADD COLUMN notify_dingtalk BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE codex_sop_task_definitions
  ADD COLUMN last_schedule_date DATE NULL;
ALTER TABLE codex_sop_task_definitions
  ADD COLUMN active_workflow_id VARCHAR(128) NULL;

CREATE INDEX idx_task_daily_schedule
  ON codex_sop_task_definitions(schedule_enabled, schedule_time, last_schedule_date);
CREATE INDEX idx_task_active_workflow
  ON codex_sop_task_definitions(active_workflow_id);

UPDATE codex_sop_task_definitions
SET active_workflow_id = dingtalk_active_workflow_id
WHERE dingtalk_active_workflow_id IS NOT NULL;

UPDATE codex_sop_task_definitions
SET active_workflow_id = (
  SELECT binding.workflow_id
  FROM codex_sop_feishu_workflow_bindings binding
  JOIN codex_sop_feishu_bot_state state
    ON state.app_id = binding.app_id
   AND state.active_workflow_id = binding.workflow_id
  WHERE binding.task_definition_id = codex_sop_task_definitions.id
)
WHERE active_workflow_id IS NULL
AND EXISTS (
  SELECT 1
  FROM codex_sop_feishu_workflow_bindings binding
  JOIN codex_sop_feishu_bot_state state
    ON state.app_id = binding.app_id
   AND state.active_workflow_id = binding.workflow_id
  WHERE binding.task_definition_id = codex_sop_task_definitions.id
);

ALTER TABLE codex_sop_dingtalk_workflow_bindings
  ADD COLUMN trigger_source VARCHAR(16) NOT NULL DEFAULT 'dingtalk';
ALTER TABLE codex_sop_dingtalk_workflow_bindings
  MODIFY COLUMN trigger_message_id VARCHAR(256) NULL;
ALTER TABLE codex_sop_dingtalk_workflow_bindings
  MODIFY COLUMN root_message_id VARCHAR(256) NULL;
ALTER TABLE codex_sop_dingtalk_workflow_bindings
  MODIFY COLUMN initiator_user_id VARCHAR(256) NULL;
