CREATE INDEX idx_dingtalk_outbox_order_scope
  ON codex_sop_dingtalk_outbox(workflow_id, conversation_id, status, delivery_order);
CREATE INDEX idx_dingtalk_inbound_workflow_status
  ON codex_sop_dingtalk_inbound_messages(workflow_id, status);
CREATE INDEX idx_dingtalk_binding_pollable
  ON codex_sop_dingtalk_workflow_bindings(client_id, status, waiting_assistant, created_at);
