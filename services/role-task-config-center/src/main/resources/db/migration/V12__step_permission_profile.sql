ALTER TABLE codex_sop_steps
  ADD COLUMN permission_profile VARCHAR(32) NOT NULL DEFAULT 'read_only'
  COMMENT '节点权限档位';

UPDATE codex_sop_steps
SET permission_profile = 'workspace_write'
WHERE write_enabled = TRUE;
