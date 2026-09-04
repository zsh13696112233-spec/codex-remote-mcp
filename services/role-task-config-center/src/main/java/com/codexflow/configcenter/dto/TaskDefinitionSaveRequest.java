package com.codexflow.configcenter.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

/**
 * 任务定义创建和更新共用的请求数据。
 *
 * @param name 任务名称
 * @param objective 任务目标
 * @param sopId 关联 SOP ID
 * @param additionalNotes 可选补充说明
 * @param enabled 是否启用
 * @param dingtalkTargetId 可选钉钉通知对象
 * @param scheduleEnabled 是否启用每天定时运行
 * @param scheduleTime 每日运行时间，格式 HH:mm
 * @param notifyDingTalk 网页或定时运行后是否推送钉钉
 */
public record TaskDefinitionSaveRequest(
    @NotBlank @Size(max = 160) String name,
    @NotBlank String objective,
    @NotBlank String sopId,
    String additionalNotes,
    Boolean enabled,
    @Size(max = 36) String dingtalkTargetId,
    Boolean scheduleEnabled,
    @Pattern(regexp = "^([01]\\d|2[0-3]):[0-5]\\d$", message = "必须使用 HH:mm 格式") String scheduleTime,
    Boolean notifyDingTalk) {

  public TaskDefinitionSaveRequest(
      String name,
      String objective,
      String sopId,
      String additionalNotes,
      Boolean enabled,
      String dingtalkTargetId) {
    this(name, objective, sopId, additionalNotes, enabled, dingtalkTargetId, null, null, null);
  }

  public TaskDefinitionSaveRequest(
      String name, String objective, String sopId, String additionalNotes, Boolean enabled) {
    this(name, objective, sopId, additionalNotes, enabled, null, null, null, null);
  }
}
