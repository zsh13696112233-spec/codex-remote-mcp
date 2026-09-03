CREATE TABLE codex_sop_dingtalk_departments (
  id VARCHAR(36) PRIMARY KEY COMMENT '本系统部门ID（UUID）',
  client_id VARCHAR(128) NOT NULL COMMENT '所属钉钉应用Client ID',
  external_id VARCHAR(64) NOT NULL COMMENT '钉钉部门ID',
  parent_external_id VARCHAR(64) NULL COMMENT '钉钉父部门ID',
  display_name VARCHAR(160) NOT NULL COMMENT '部门名称',
  available BOOLEAN NOT NULL DEFAULT TRUE COMMENT '本次同步时是否仍可见',
  last_synced_at TIMESTAMP(6) COMMENT '最近同步时间',
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
    ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  CONSTRAINT uq_dingtalk_department UNIQUE(client_id, external_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='钉钉组织部门树';

CREATE INDEX idx_dingtalk_department_tree
  ON codex_sop_dingtalk_departments(client_id, parent_external_id, available, display_name);

CREATE TABLE codex_sop_dingtalk_target_departments (
  target_id VARCHAR(36) NOT NULL COMMENT '人员通知对象ID',
  department_id VARCHAR(36) NOT NULL COMMENT '部门ID',
  PRIMARY KEY(target_id, department_id),
  CONSTRAINT fk_dingtalk_target_department_target
    FOREIGN KEY(target_id) REFERENCES codex_sop_dingtalk_targets(id),
  CONSTRAINT fk_dingtalk_target_department_department
    FOREIGN KEY(department_id) REFERENCES codex_sop_dingtalk_departments(id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='钉钉人员部门归属';

CREATE INDEX idx_dingtalk_target_department_department
  ON codex_sop_dingtalk_target_departments(department_id, target_id);
