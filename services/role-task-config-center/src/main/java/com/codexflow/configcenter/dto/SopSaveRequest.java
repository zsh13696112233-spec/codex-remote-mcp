package com.codexflow.configcenter.dto;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Size;
import java.util.List;

/**
 * SOP 创建和更新共用的请求数据。
 *
 * @param name SOP 名称
 * @param description 可选说明
 * @param supervisorTimeoutSec 主监督超时秒数
 * @param defaultStepModel 默认步骤模型
 * @param enabled 是否启用
 * @param steps 按执行顺序排列的步骤列表
 */
public record SopSaveRequest(
    @NotBlank @Size(max = 160) String name,
    @Size(max = 2000) String description,
    Integer supervisorTimeoutSec,
    @Size(max = 64) String defaultStepModel,
    Boolean enabled,
    @NotEmpty List<@Valid SopStepRequest> steps) {}
