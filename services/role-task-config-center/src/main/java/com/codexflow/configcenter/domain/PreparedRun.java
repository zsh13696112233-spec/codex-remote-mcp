package com.codexflow.configcenter.domain;

import tools.jackson.databind.node.ObjectNode;

/**
 * 已完成持久化、可以提交给工作流网关的运行信息。
 *
 * @param workflowId 工作流唯一编号
 * @param payload 发送给工作流网关的完整请求载荷
 */
public record PreparedRun(String workflowId, ObjectNode payload) {}
