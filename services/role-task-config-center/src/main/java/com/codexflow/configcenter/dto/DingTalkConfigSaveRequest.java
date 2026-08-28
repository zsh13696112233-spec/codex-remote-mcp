package com.codexflow.configcenter.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

/** 页面保存或测试钉钉机器人参数的请求。空 Client Secret 表示沿用已保存的值。 */
public record DingTalkConfigSaveRequest(
    @NotNull Boolean enabled,
    @NotBlank @Size(max = 128) String clientId,
    @Size(max = 512) String clientSecret,
    @NotBlank
        @Pattern(
            regexp =
                "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
        String taskDefinitionId,
    @NotNull @Size(max = 256) String cardTemplateId,
    @NotNull @Min(250) @Max(60000) Long eventPollIntervalMs) {}
