package com.codexflow.configcenter.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import java.util.Set;

/**
 * 一个串行 SOP 步骤的可编辑请求数据。
 *
 * @param displayName 步骤显示名称
 * @param roleId 执行角色 ID
 * @param instruction 步骤执行指令
 * @param expectedOutput 预期输出说明
 * @param executorType 执行器类型
 * @param agentId 执行机 ID
 * @param workingDirectory 可选工作目录
 * @param writeEnabled 是否允许文件写入
 * @param permissionProfile 节点权限档位
 * @param modelOverride 可选模型覆盖值
 * @param timeoutSec 步骤超时秒数
 * @param skills Skill 标签集合
 * @param mcps MCP 标签集合
 */
public record SopStepRequest(
    @NotBlank @Size(max = 160) String displayName,
    @NotBlank String roleId,
    @NotBlank String instruction,
    String expectedOutput,
    String executorType,
    @NotBlank @Size(max = 128) String agentId,
    @Size(max = 1000) String workingDirectory,
    Boolean writeEnabled,
    @Size(max = 32) String permissionProfile,
    @Size(max = 64) String modelOverride,
    Integer timeoutSec,
    Set<@Size(max = 160) String> skills,
    Set<@Size(max = 160) String> mcps) {}
