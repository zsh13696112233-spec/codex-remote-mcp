ALTER TABLE codex_sop_sops
  ADD COLUMN deleted BOOLEAN NOT NULL DEFAULT FALSE
  COMMENT '软删除标记；保留历史任务定义的外键引用';
