ALTER TABLE codex_sop_dingtalk_outbox ADD COLUMN waiting_card_state VARCHAR(16) NULL;
CREATE INDEX idx_dingtalk_waiting_cards ON codex_sop_dingtalk_outbox (workflow_id, waiting_card_state);
ALTER TABLE codex_sop_dingtalk_inbound_messages ADD COLUMN observed_gate_id VARCHAR(128) NULL;
