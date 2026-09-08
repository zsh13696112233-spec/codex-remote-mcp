CREATE INDEX idx_task_notification_target ON codex_sop_task_definitions(dingtalk_target_id);
ALTER TABLE codex_sop_task_definitions DROP INDEX uq_task_dingtalk_target;
ALTER TABLE codex_sop_dingtalk_inbound_messages ADD COLUMN conversation_id VARCHAR(256) NULL;
ALTER TABLE codex_sop_dingtalk_inbound_messages ADD COLUMN conversation_type VARCHAR(16) NULL;
ALTER TABLE codex_sop_dingtalk_inbound_messages ADD COLUMN image_ids_json LONGTEXT NULL;
ALTER TABLE codex_sop_dingtalk_inbound_messages ADD COLUMN session_webhook LONGTEXT NULL;
ALTER TABLE codex_sop_dingtalk_inbound_messages ADD COLUMN action_id VARCHAR(128) NULL;
