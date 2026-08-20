UPDATE codex_sop_sops
SET supervisor_agent_id = 'local'
WHERE supervisor_agent_id <> 'local';
