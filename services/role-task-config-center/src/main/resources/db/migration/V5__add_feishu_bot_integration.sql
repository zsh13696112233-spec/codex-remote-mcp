CREATE TABLE codex_sop_feishu_bot_state (
  app_id VARCHAR(128) PRIMARY KEY COMMENT '飞书应用ID',
  active_workflow_id VARCHAR(128) COMMENT '当前占用机器人的工作流ID',
  version BIGINT NOT NULL DEFAULT 0 COMMENT '并发控制版本',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间'
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='飞书机器人全局单任务状态';

CREATE TABLE codex_sop_feishu_workflow_bindings (
  workflow_id VARCHAR(128) PRIMARY KEY COMMENT '工作流ID',
  app_id VARCHAR(128) NOT NULL COMMENT '飞书应用ID',
  task_definition_id VARCHAR(36) NOT NULL COMMENT '固定任务定义ID',
  trigger_message_id VARCHAR(256) NOT NULL COMMENT '启动任务的飞书消息ID',
  chat_id VARCHAR(256) NOT NULL COMMENT '飞书群聊ID',
  root_message_id VARCHAR(256) NOT NULL COMMENT '任务话题根消息ID',
  thread_id VARCHAR(256) COMMENT '飞书话题ID',
  initiator_open_id VARCHAR(256) NOT NULL COMMENT '任务发起人open_id',
  status VARCHAR(32) NOT NULL COMMENT 'submitting、active、terminal或failed',
  event_cursor BIGINT NOT NULL DEFAULT 0 COMMENT '已持久化处理的运行事件游标',
  progress_message_id VARCHAR(256) COMMENT '飞书进度卡片消息ID',
  waiting_assistant BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否等待任务助手回复',
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间',
  CONSTRAINT fk_feishu_binding_task FOREIGN KEY(task_definition_id)
    REFERENCES codex_sop_task_definitions(id),
  CONSTRAINT fk_feishu_binding_run FOREIGN KEY(workflow_id)
    REFERENCES codex_sop_task_runs(workflow_id),
  CONSTRAINT uq_feishu_trigger UNIQUE(app_id, trigger_message_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='飞书话题与工作流绑定';

CREATE INDEX idx_feishu_binding_status
  ON codex_sop_feishu_workflow_bindings(app_id, status, updated_at);
CREATE INDEX idx_feishu_binding_conversation
  ON codex_sop_feishu_workflow_bindings(app_id, chat_id, root_message_id);
CREATE INDEX idx_feishu_binding_thread
  ON codex_sop_feishu_workflow_bindings(app_id, chat_id, thread_id);

CREATE TABLE codex_sop_feishu_inbound_messages (
  message_id VARCHAR(256) PRIMARY KEY COMMENT '飞书消息ID',
  workflow_id VARCHAR(128) NOT NULL COMMENT '所属工作流ID',
  workflow_message_id VARCHAR(36) NOT NULL COMMENT '发送给任务助手的确定性UUID',
  sender_open_id VARCHAR(256) NOT NULL COMMENT '发送人open_id',
  status VARCHAR(32) NOT NULL COMMENT 'accepted、completed或failed',
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间',
  CONSTRAINT fk_feishu_inbound_binding FOREIGN KEY(workflow_id)
    REFERENCES codex_sop_feishu_workflow_bindings(workflow_id),
  CONSTRAINT uq_feishu_workflow_message UNIQUE(workflow_id, workflow_message_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='飞书任务助手消息幂等映射';

CREATE TABLE codex_sop_feishu_outbox (
  id VARCHAR(36) PRIMARY KEY COMMENT '待发送消息ID',
  dedup_key VARCHAR(512) NOT NULL COMMENT '业务幂等键',
  workflow_id VARCHAR(128) COMMENT '关联工作流ID',
  chat_id VARCHAR(256) NOT NULL COMMENT '飞书群聊ID',
  reply_to_message_id VARCHAR(256) COMMENT '回复目标消息ID',
  message_kind VARCHAR(32) NOT NULL COMMENT 'text、card或card_update',
  payload_json LONGTEXT NOT NULL COMMENT '待发送内容JSON',
  status VARCHAR(32) NOT NULL COMMENT 'pending、sending、sent或failed',
  attempt_count INT NOT NULL DEFAULT 0 COMMENT '发送尝试次数',
  next_attempt_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '下次尝试时间',
  last_error VARCHAR(2000) COMMENT '最近一次发送错误',
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间',
  CONSTRAINT uq_feishu_outbox_dedup UNIQUE(dedup_key),
  CONSTRAINT fk_feishu_outbox_binding FOREIGN KEY(workflow_id)
    REFERENCES codex_sop_feishu_workflow_bindings(workflow_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='飞书可靠发送Outbox';

CREATE INDEX idx_feishu_outbox_pending
  ON codex_sop_feishu_outbox(status, next_attempt_at, created_at);
