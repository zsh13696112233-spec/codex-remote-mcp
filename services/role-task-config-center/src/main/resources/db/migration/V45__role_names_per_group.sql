-- NULL 分组统一映射为空串，使历史待归组角色仍有名称唯一保护。
ALTER TABLE codex_sop_roles ADD COLUMN name_group VARCHAR(36) GENERATED ALWAYS AS (COALESCE(group_id, ''));
ALTER TABLE codex_sop_roles ADD COLUMN active_group_name VARCHAR(100) GENERATED ALWAYS AS (CASE WHEN deleted=FALSE THEN LOWER(name) ELSE NULL END);
CREATE UNIQUE INDEX uq_roles_group_active_name ON codex_sop_roles(name_group, active_group_name);
DROP INDEX uq_roles_active_name ON codex_sop_roles;
ALTER TABLE codex_sop_roles DROP COLUMN active_name;
