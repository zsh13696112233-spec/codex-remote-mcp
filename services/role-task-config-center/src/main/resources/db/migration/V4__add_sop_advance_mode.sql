ALTER TABLE codex_sop_sops
  ADD COLUMN advance_mode VARCHAR(32) NOT NULL DEFAULT 'automatic'
  COMMENT '成功步骤后的流转方式：automatic或semi_automatic';
