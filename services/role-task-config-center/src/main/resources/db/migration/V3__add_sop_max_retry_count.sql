ALTER TABLE codex_sop_sops
  ADD COLUMN max_retry_count INT NOT NULL DEFAULT 10 COMMENT '单次运行允许的人工重跑总次数';
