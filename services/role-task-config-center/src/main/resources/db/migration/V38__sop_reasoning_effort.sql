ALTER TABLE codex_sop_sops ADD COLUMN default_step_reasoning_effort VARCHAR(32) NULL;
ALTER TABLE codex_sop_steps ADD COLUMN reasoning_effort_override VARCHAR(32) NULL;
