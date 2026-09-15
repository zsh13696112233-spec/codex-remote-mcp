package com.codexflow.configcenter.dto;

import jakarta.validation.constraints.*;

public record TaskScheduleRequest(
    @NotBlank @Size(max = 160) String name,
    @NotBlank String sopId,
    @NotBlank String taskDefinitionId,
    @NotBlank @Pattern(regexp = "daily|interval") String mode,
    @Pattern(regexp = "([01]\\d|2[0-3]):[0-5]\\d") String dailyTime,
    @Min(5) @Max(1440) Integer intervalMinutes,
    @NotNull Boolean enabled,
    @NotBlank @Size(max = 36) String groupId) {
  public TaskScheduleRequest(
      String name,
      String sopId,
      String taskDefinitionId,
      String mode,
      String dailyTime,
      Integer intervalMinutes,
      Boolean enabled) {
    this(name, sopId, taskDefinitionId, mode, dailyTime, intervalMinutes, enabled, null);
  }
}
