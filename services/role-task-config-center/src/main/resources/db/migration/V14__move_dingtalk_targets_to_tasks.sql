ALTER TABLE codex_sop_task_definitions ADD COLUMN dingtalk_target_id VARCHAR(36) NULL;
ALTER TABLE codex_sop_task_definitions ADD COLUMN dingtalk_active_workflow_id VARCHAR(128) NULL;

UPDATE codex_sop_task_definitions
SET dingtalk_target_id = (
  SELECT sop.dingtalk_target_id
  FROM codex_sop_sops sop
  WHERE sop.id = codex_sop_task_definitions.sop_id
)
WHERE id = (
  SELECT settings.task_definition_id
  FROM codex_sop_dingtalk_bot_settings settings
  WHERE settings.id = 1
)
AND dingtalk_target_id IS NULL;

UPDATE codex_sop_task_definitions
SET dingtalk_active_workflow_id = (
  SELECT state.active_workflow_id
  FROM codex_sop_dingtalk_bot_state state
  JOIN codex_sop_dingtalk_bot_settings settings ON settings.client_id = state.client_id
  WHERE settings.id = 1
)
WHERE id = (
  SELECT settings.task_definition_id
  FROM codex_sop_dingtalk_bot_settings settings
  WHERE settings.id = 1
);

UPDATE codex_sop_sops SET dingtalk_target_id = NULL WHERE dingtalk_target_id IS NOT NULL;

ALTER TABLE codex_sop_dingtalk_bot_settings DROP FOREIGN KEY fk_dingtalk_settings_task;
ALTER TABLE codex_sop_dingtalk_bot_settings
  MODIFY COLUMN task_definition_id VARCHAR(36) NULL;
UPDATE codex_sop_dingtalk_bot_settings SET task_definition_id = NULL;

ALTER TABLE codex_sop_task_definitions ADD CONSTRAINT fk_task_dingtalk_target
  FOREIGN KEY(dingtalk_target_id) REFERENCES codex_sop_dingtalk_targets(id);
ALTER TABLE codex_sop_task_definitions ADD CONSTRAINT uq_task_dingtalk_target UNIQUE(dingtalk_target_id);
CREATE INDEX idx_task_dingtalk_active
  ON codex_sop_task_definitions(dingtalk_active_workflow_id);
