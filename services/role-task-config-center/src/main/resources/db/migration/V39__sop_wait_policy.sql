ALTER TABLE codex_sop_sops ADD COLUMN confirmation_minutes INT NOT NULL DEFAULT 2;
ALTER TABLE codex_sop_sops ADD COLUMN blocked_minutes INT NOT NULL DEFAULT 30;
