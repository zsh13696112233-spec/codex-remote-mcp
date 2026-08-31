ALTER TABLE codex_sop_roles
  ADD COLUMN deleted BOOLEAN NOT NULL DEFAULT FALSE
  COMMENT '软删除标记；保留历史SOP步骤的外键引用';
