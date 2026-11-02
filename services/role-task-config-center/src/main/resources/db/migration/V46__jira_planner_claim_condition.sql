-- 原标准查询固定以当前登录负责人结尾；保留项目与状态，给所有 SOP 补齐 Planner。
-- 已包含处理角色条件的查询及历史运行快照不改写。
UPDATE codex_sop_sops
SET jira_jql = CONCAT(TRIM(jira_jql), ' AND "处理角色" = "Planner"'),
    updated_at = CURRENT_TIMESTAMP(6)
WHERE jira_jql IS NOT NULL
  AND TRIM(jira_jql) LIKE '%assignee = currentUser()';
