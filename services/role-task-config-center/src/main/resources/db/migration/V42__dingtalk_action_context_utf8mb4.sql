-- V41 可能继承数据库的 latin1 默认字符集，导致中文操作人姓名无法保存。
-- 保留已有操作记录、主键与幂等唯一约束，不修改已应用的 V41。
ALTER TABLE codex_sop_dingtalk_action_contexts
    CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
