package com.codexflow.configcenter.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/** 仅保存查询条件和账号别名；凭据来自监督机已配置的 Jira CLI。 */
public record JiraDispatchRequest(
    @NotBlank @Size(max = 4000) String jql, @Size(max = 128) String account) {}
