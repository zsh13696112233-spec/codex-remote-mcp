-- 仅补齐 SOP 的当前配置；历史运行与提交快照保持不变。
UPDATE codex_sop_sops
SET jira_jql = CONCAT(jira_jql, ' AND issuetype = "sub-bug"')
WHERE jira_jql IS NOT NULL AND TRIM(jira_jql) <> ''
  AND LOWER(jira_jql) NOT LIKE '%issuetype%';
