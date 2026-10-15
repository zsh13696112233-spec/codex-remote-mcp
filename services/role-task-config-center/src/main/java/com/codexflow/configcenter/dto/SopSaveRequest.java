package com.codexflow.configcenter.dto;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
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
 * @param supervisorAgentId 主监督执行机 ID
 * @param supervisorTimeoutSec 主监督超时秒数
 * @param defaultStepModel 默认步骤模型
 * @param enabled 是否启用
 * @param advanceMode 步骤成功后的流转方式
 * @param handoffMode 步骤之间的结果交接方式
 * @param dingtalkTargetId V1 兼容字段，当前忽略；通知对象由任务定义维护
 * @param dingtalkShowExecutionDetails 是否向钉钉展示思考摘要和工具调用
 * @param steps 按执行顺序排列的步骤列表
 */
@JsonIgnoreProperties({"maxRetryCount", "confirmationMinutes", "blockedMinutes"})
public record SopSaveRequest(
    @NotBlank @Size(max = 160) String name,
    @Size(max = 2000) String description,
    @NotBlank @Size(max = 128) String supervisorAgentId,
    Integer supervisorTimeoutSec,
    @Size(max = 64) String defaultStepModel,
    Boolean enabled,
    @Size(max = 32) String advanceMode,
    @Size(max = 32) String handoffMode,
    @Size(max = 36) String dingtalkTargetId,
    @NotEmpty List<@Valid SopStepRequest> steps,
    @NotBlank @Size(max = 36) String groupId,
    Boolean dingtalkShowExecutionDetails,
    @Size(max = 32) String defaultStepReasoningEffort) {
  public SopSaveRequest(
      String name,
      String description,
      String supervisorAgentId,
      Integer supervisorTimeoutSec,
      String defaultStepModel,
      Boolean enabled,
      String advanceMode,
      String handoffMode,
      String dingtalkTargetId,
      List<SopStepRequest> steps,
      String groupId,
      Boolean dingtalkShowExecutionDetails) {
    this(
        name,
        description,
        supervisorAgentId,
        supervisorTimeoutSec,
        defaultStepModel,
        enabled,
        advanceMode,
        handoffMode,
        dingtalkTargetId,
        steps,
        groupId,
        dingtalkShowExecutionDetails,
        null);
  }

  public SopSaveRequest(
      String name,
      String description,
      String supervisorAgentId,
      Integer supervisorTimeoutSec,
      String defaultStepModel,
      Boolean enabled,
      String advanceMode,
      String handoffMode,
      String dingtalkTargetId,
      List<SopStepRequest> steps,
      String groupId) {
    this(
        name,
        description,
        supervisorAgentId,
        supervisorTimeoutSec,
        defaultStepModel,
        enabled,
        advanceMode,
        handoffMode,
        dingtalkTargetId,
        steps,
        groupId,
        null);
  }

  public SopSaveRequest(
      String name,
      String description,
      String supervisorAgentId,
      Integer supervisorTimeoutSec,
      String defaultStepModel,
      Boolean enabled,
      String advanceMode,
      String handoffMode,
      String dingtalkTargetId,
      List<SopStepRequest> steps) {
    this(
        name,
        description,
        supervisorAgentId,
        supervisorTimeoutSec,
        defaultStepModel,
        enabled,
        advanceMode,
        handoffMode,
        dingtalkTargetId,
        steps,
        null);
  }

  public SopSaveRequest(
      String name,
      String description,
      String supervisorAgentId,
      Integer supervisorTimeoutSec,
      String defaultStepModel,
      Boolean enabled,
      String advanceMode,
      String handoffMode,
      List<SopStepRequest> steps) {
    this(
        name,
        description,
        supervisorAgentId,
        supervisorTimeoutSec,
        defaultStepModel,
        enabled,
        advanceMode,
        handoffMode,
        null,
        steps);
  }

  public SopSaveRequest(
      String name,
      String description,
      Integer supervisorTimeoutSec,
      String defaultStepModel,
      Boolean enabled,
      List<SopStepRequest> steps) {
    this(
        name,
        description,
        "local",
        supervisorTimeoutSec,
        defaultStepModel,
        enabled,
        null,
        null,
        null,
        steps);
  }

  public SopSaveRequest(
      String name,
      String description,
      Integer supervisorTimeoutSec,
      String defaultStepModel,
      Boolean enabled,
      String advanceMode,
      List<SopStepRequest> steps) {
    this(
        name,
        description,
        "local",
        supervisorTimeoutSec,
        defaultStepModel,
        enabled,
        advanceMode,
        null,
        null,
        steps);
  }

  public SopSaveRequest(
      String name,
      String description,
      Integer supervisorTimeoutSec,
      String defaultStepModel,
      Boolean enabled,
      String advanceMode,
      String handoffMode,
      List<SopStepRequest> steps) {
    this(
        name,
        description,
        "local",
        supervisorTimeoutSec,
        defaultStepModel,
        enabled,
        advanceMode,
        handoffMode,
        null,
        steps);
  }
}
