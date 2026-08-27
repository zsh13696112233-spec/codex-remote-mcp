CREATE TABLE codex_sop_dingtalk_bot_state (
  client_id VARCHAR(128) PRIMARY KEY COMMENT '钉钉应用Client ID',
  active_workflow_id VARCHAR(128) COMMENT '当前占用机器人的工作流ID',
  version BIGINT NOT NULL DEFAULT 0 COMMENT '并发控制版本',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间'
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='钉钉机器人全局单任务状态';

CREATE TABLE codex_sop_dingtalk_workflow_bindings (
  workflow_id VARCHAR(128) PRIMARY KEY COMMENT '工作流ID',
  client_id VARCHAR(128) NOT NULL COMMENT '钉钉应用Client ID',
  task_definition_id VARCHAR(36) NOT NULL COMMENT '固定任务定义ID',
  trigger_message_id VARCHAR(256) NOT NULL COMMENT '启动任务的钉钉消息ID',
  conversation_id VARCHAR(256) NOT NULL COMMENT '钉钉群会话ID',
  root_message_id VARCHAR(256) NOT NULL COMMENT '任务回复链根消息ID',
  initiator_user_id VARCHAR(256) NOT NULL COMMENT '任务发起人用户ID',
  status VARCHAR(32) NOT NULL COMMENT 'submitting、active、terminal或failed',
  event_cursor BIGINT NOT NULL DEFAULT 0 COMMENT '已持久化处理的运行事件游标',
  progress_card_instance_id VARCHAR(256) COMMENT '钉钉进度卡实例ID',
  waiting_assistant BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否等待任务助手回复',
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间',
  CONSTRAINT fk_dingtalk_binding_task FOREIGN KEY(task_definition_id)
    REFERENCES codex_sop_task_definitions(id),
  CONSTRAINT fk_dingtalk_binding_run FOREIGN KEY(workflow_id)
    REFERENCES codex_sop_task_runs(workflow_id),
  CONSTRAINT uq_dingtalk_trigger UNIQUE(client_id, trigger_message_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='钉钉回复链与工作流绑定';

CREATE INDEX idx_dingtalk_binding_status
  ON codex_sop_dingtalk_workflow_bindings(client_id, status, updated_at);
CREATE INDEX idx_dingtalk_binding_root
  ON codex_sop_dingtalk_workflow_bindings(client_id, conversation_id, root_message_id);
CREATE INDEX idx_dingtalk_binding_card
  ON codex_sop_dingtalk_workflow_bindings(client_id, conversation_id, progress_card_instance_id);

CREATE TABLE codex_sop_dingtalk_inbound_messages (
  message_id VARCHAR(256) PRIMARY KEY COMMENT '钉钉消息ID',
  workflow_id VARCHAR(128) NOT NULL COMMENT '所属工作流ID',
  workflow_message_id VARCHAR(36) NOT NULL COMMENT '发送给任务助手的确定性UUID',
  sender_user_id VARCHAR(256) NOT NULL COMMENT '发送人用户ID',
  status VARCHAR(32) NOT NULL COMMENT 'accepted、completed或failed',
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间',
  CONSTRAINT fk_dingtalk_inbound_binding FOREIGN KEY(workflow_id)
    REFERENCES codex_sop_dingtalk_workflow_bindings(workflow_id),
  CONSTRAINT uq_dingtalk_workflow_message UNIQUE(workflow_id, workflow_message_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='钉钉任务助手消息幂等映射';

CREATE TABLE codex_sop_dingtalk_outbox (
  id VARCHAR(36) PRIMARY KEY COMMENT '待发送消息ID',
  dedup_key VARCHAR(512) NOT NULL COMMENT '业务幂等键',
  workflow_id VARCHAR(128) COMMENT '关联工作流ID',
  conversation_id VARCHAR(256) NOT NULL COMMENT '钉钉群会话ID',
  reply_to_message_id VARCHAR(256) COMMENT '业务回复目标消息ID',
  message_kind VARCHAR(32) NOT NULL COMMENT 'text、card或card_update',
  payload_json LONGTEXT NOT NULL COMMENT '待发送内容JSON',
  status VARCHAR(32) NOT NULL COMMENT 'pending、sending、sent或failed',
  attempt_count INT NOT NULL DEFAULT 0 COMMENT '发送尝试次数',
  next_attempt_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '下次尝试时间',
  last_error VARCHAR(2000) COMMENT '最近一次发送错误',
  sent_message_id VARCHAR(256) COMMENT '钉钉返回的消息或卡片标识，用于引用路由',
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间',
  CONSTRAINT uq_dingtalk_outbox_dedup UNIQUE(dedup_key),
  CONSTRAINT fk_dingtalk_outbox_binding FOREIGN KEY(workflow_id)
    REFERENCES codex_sop_dingtalk_workflow_bindings(workflow_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='钉钉可靠发送Outbox';

CREATE INDEX idx_dingtalk_outbox_pending
  ON codex_sop_dingtalk_outbox(status, next_attempt_at, created_at);

CREATE INDEX idx_dingtalk_outbox_sent_message
  ON codex_sop_dingtalk_outbox(conversation_id, sent_message_id, created_at);

CREATE TABLE codex_sop_dingtalk_bot_settings (
  id TINYINT PRIMARY KEY COMMENT '单机器人配置固定主键',
  enabled BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否启用Stream长连接',
  client_id VARCHAR(128) NOT NULL COMMENT '钉钉应用Client ID',
  client_secret VARCHAR(512) NOT NULL COMMENT '钉钉应用Client Secret，仅服务端使用且接口不回显',
  task_definition_id VARCHAR(36) NOT NULL COMMENT '固定任务定义ID',
  card_template_id VARCHAR(256) NOT NULL COMMENT '互动进度卡模板ID',
  event_poll_interval_ms BIGINT NOT NULL DEFAULT 1000 COMMENT '事件与Outbox轮询间隔毫秒',
  version BIGINT NOT NULL DEFAULT 0 COMMENT '乐观锁版本',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间',
  CONSTRAINT fk_dingtalk_settings_task FOREIGN KEY(task_definition_id)
    REFERENCES codex_sop_task_definitions(id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='钉钉机器人运行配置';
