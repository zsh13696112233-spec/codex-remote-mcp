ALTER TABLE codex_sop_sops ADD COLUMN editor_graph_json LONGTEXT;
ALTER TABLE codex_sop_steps ADD COLUMN node_key VARCHAR(128);
UPDATE codex_sop_steps SET node_key = id WHERE node_key IS NULL;
