ALTER TABLE codex_sop_dingtalk_inbound_messages ADD COLUMN question_text LONGTEXT;
ALTER TABLE codex_sop_dingtalk_inbound_messages ADD COLUMN sender_display_name VARCHAR(160);

CREATE TABLE codex_sop_dingtalk_action_contexts (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    workflow_id VARCHAR(128) NOT NULL,
    gate_id VARCHAR(128) NOT NULL,
    action_name VARCHAR(32) NOT NULL,
    operator_user_id VARCHAR(256) NOT NULL,
    operator_display_name VARCHAR(160) NOT NULL,
    created_at TIMESTAMP(6) NOT NULL,
    UNIQUE KEY uk_dingtalk_action_context (workflow_id, gate_id, action_name, operator_user_id)
);
