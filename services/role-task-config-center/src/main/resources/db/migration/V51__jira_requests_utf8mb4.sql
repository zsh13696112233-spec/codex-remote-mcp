-- V47 未指定字符集，在 latin1 默认库中无法保存中文任务快照、原因或钉钉消息。
-- 保留已有请求、主键和幂等约束，不修改已应用的 V47。
ALTER TABLE codex_jira_requests
    CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE codex_jira_dingtalk_requests
    CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
