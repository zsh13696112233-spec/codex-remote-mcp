package com.codexflow.configcenter.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 角色创建和更新共用的请求数据。
 *
 * @param name 角色名称
 * @param duty 角色职责说明
 * @param enabled 是否启用
 * @param version 更新时使用的乐观锁版本；创建时可为空
 */
public record RoleSaveRequest(
    @NotBlank @Size(max = 100) String name,
    @NotBlank @Size(max = 50000) String duty,
    Boolean enabled,
    Long version,
    @NotBlank @Size(max = 36) String groupId) {
  public RoleSaveRequest(String name, String duty, Boolean enabled, Long version) {
    this(name, duty, enabled, version, null);
  }
}
