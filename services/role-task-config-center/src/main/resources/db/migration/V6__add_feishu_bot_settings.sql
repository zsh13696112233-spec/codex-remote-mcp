CREATE TABLE codex_sop_feishu_bot_settings (
  id TINYINT PRIMARY KEY COMMENT '单机器人配置固定主键',
  enabled BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否启用长连接',
  app_id VARCHAR(128) NOT NULL COMMENT '飞书应用ID',
  app_secret VARCHAR(512) NOT NULL COMMENT '飞书应用密钥，仅服务端使用且接口不回显',
  task_definition_id VARCHAR(36) NOT NULL COMMENT '固定任务定义ID',
  event_poll_interval_ms BIGINT NOT NULL DEFAULT 1000 COMMENT '事件与Outbox轮询间隔毫秒',
  version BIGINT NOT NULL DEFAULT 0 COMMENT '乐观锁版本',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间',
  CONSTRAINT fk_feishu_settings_task FOREIGN KEY(task_definition_id)
    REFERENCES codex_sop_task_definitions(id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='飞书机器人运行配置';
