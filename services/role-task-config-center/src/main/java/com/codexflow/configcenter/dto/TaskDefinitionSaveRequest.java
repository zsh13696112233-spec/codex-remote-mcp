package com.codexflow.configcenter.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 任务定义创建和更新共用的请求数据。
 *
 * @param name 任务名称
 * @param objective 任务目标
 * @param sopId 关联 SOP ID
 * @param additionalNotes 可选补充说明
 * @param enabled 是否启用
 */
public record TaskDefinitionSaveRequest(
    @NotBlank @Size(max = 160) String name,
    @NotBlank String objective,
    @NotBlank String sopId,
    String additionalNotes,
    Boolean enabled) {}
