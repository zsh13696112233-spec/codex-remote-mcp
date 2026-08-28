ALTER TABLE codex_sop_dingtalk_workflow_bindings
  ADD COLUMN latest_assistant_reply LONGTEXT NULL COMMENT '最近一次任务助手完整回复，用于刷新进度卡';

ALTER TABLE codex_sop_dingtalk_workflow_bindings
  ADD COLUMN latest_assistant_reply_at TIMESTAMP(6) NULL COMMENT '最近一次任务助手回复时间';
