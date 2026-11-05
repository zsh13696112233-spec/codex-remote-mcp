package com.codexflow.configcenter.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/** 保存查询条件与中央账号引用；旧账号别名仅用于历史读取。 */
public record JiraDispatchRequest(
    @NotBlank @Size(max = 4000) String jql,
    @Size(max = 128) String account,
    @Size(max = 36) String connectionId) {}
