ALTER TABLE codex_sop_dingtalk_outbox ADD COLUMN advance_gate_id VARCHAR(128);
ALTER TABLE codex_sop_dingtalk_outbox ADD COLUMN delivered_at TIMESTAMP(6) NULL DEFAULT NULL;
