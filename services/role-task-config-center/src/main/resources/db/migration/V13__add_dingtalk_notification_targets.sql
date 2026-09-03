CREATE TABLE codex_sop_dingtalk_targets (
  id VARCHAR(36) PRIMARY KEY COMMENT '通知对象ID（UUID）',
  client_id VARCHAR(128) NOT NULL COMMENT '所属钉钉应用Client ID',
  target_type VARCHAR(16) NOT NULL COMMENT 'PERSON或GROUP',
  external_id VARCHAR(256) NOT NULL COMMENT '人员userId或群openConversationId',
  display_name VARCHAR(160) NOT NULL COMMENT '人员姓名或群显示名称',
  department_display VARCHAR(1000) COMMENT '人员所属部门展示文本',
  source VARCHAR(16) NOT NULL COMMENT 'DIRECTORY或OBSERVED',
  available BOOLEAN NOT NULL DEFAULT TRUE COMMENT '钉钉侧是否仍可用',
  enabled BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否允许SOP选择',
  deleted BOOLEAN NOT NULL DEFAULT FALSE COMMENT '软删除标记',
  last_synced_at TIMESTAMP(6) COMMENT '最近同步或发现时间',
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  CONSTRAINT uq_dingtalk_target UNIQUE(client_id, target_type, external_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='钉钉人员与群通知对象';

CREATE INDEX idx_dingtalk_target_list
  ON codex_sop_dingtalk_targets(client_id, target_type, deleted, enabled, display_name);

ALTER TABLE codex_sop_sops ADD COLUMN dingtalk_target_id VARCHAR(36) NULL;
ALTER TABLE codex_sop_sops ADD CONSTRAINT fk_sop_dingtalk_target
  FOREIGN KEY(dingtalk_target_id) REFERENCES codex_sop_dingtalk_targets(id);

ALTER TABLE codex_sop_dingtalk_workflow_bindings ADD COLUMN target_type VARCHAR(16) NULL;
ALTER TABLE codex_sop_dingtalk_workflow_bindings ADD COLUMN target_external_id VARCHAR(256) NULL;
ALTER TABLE codex_sop_dingtalk_workflow_bindings ADD COLUMN target_name VARCHAR(160) NULL;
UPDATE codex_sop_dingtalk_workflow_bindings
SET target_type = 'GROUP', target_external_id = conversation_id, target_name = '历史群聊'
WHERE target_type IS NULL;

ALTER TABLE codex_sop_dingtalk_outbox ADD COLUMN target_type VARCHAR(16) NULL;
ALTER TABLE codex_sop_dingtalk_outbox ADD COLUMN target_external_id VARCHAR(256) NULL;
UPDATE codex_sop_dingtalk_outbox
SET target_type = 'GROUP', target_external_id = conversation_id
WHERE target_type IS NULL;
