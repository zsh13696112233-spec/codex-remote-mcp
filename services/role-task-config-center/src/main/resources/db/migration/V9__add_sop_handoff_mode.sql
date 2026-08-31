ALTER TABLE codex_sop_sops
  ADD COLUMN handoff_mode VARCHAR(32) NOT NULL DEFAULT 'cumulative_files'
  COMMENT '步骤结果交接方式：legacy_text或cumulative_files';
