ALTER TABLE codex_sop_dingtalk_workflow_bindings ADD COLUMN session_webhook LONGTEXT NULL;
ALTER TABLE codex_sop_dingtalk_outbox ADD COLUMN delivery_order BIGINT NOT NULL AUTO_INCREMENT UNIQUE;
