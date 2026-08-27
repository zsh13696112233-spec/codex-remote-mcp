package com.codexflow.configcenter.integration.feishu;

import java.util.UUID;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/** 飞书机器人长连接和固定任务绑定配置。 */
@Component
@ConfigurationProperties(prefix = "feishu")
public class FeishuProperties {

  private boolean enabled;
  private String appId = "";
  private String appSecret = "";
  private String taskDefinitionId = "";
  private long eventPollIntervalMs = 1000;

  public boolean isEnabled() {
    return enabled;
  }

  public void setEnabled(boolean enabled) {
    this.enabled = enabled;
  }

  public String getAppId() {
    return appId;
  }

  public void setAppId(String appId) {
    this.appId = appId == null ? "" : appId.trim();
  }

  public String getAppSecret() {
    return appSecret;
  }

  public void setAppSecret(String appSecret) {
    this.appSecret = appSecret == null ? "" : appSecret.trim();
  }

  public String getTaskDefinitionId() {
    return taskDefinitionId;
  }

  public void setTaskDefinitionId(String taskDefinitionId) {
    this.taskDefinitionId = taskDefinitionId == null ? "" : taskDefinitionId.trim();
  }

  public long getEventPollIntervalMs() {
    return eventPollIntervalMs;
  }

  public void setEventPollIntervalMs(long eventPollIntervalMs) {
    this.eventPollIntervalMs = eventPollIntervalMs;
  }

  /** 启用时校验所有必填配置和轮询范围。 */
  public void validateEnabledConfiguration() {
    if (!enabled) return;
    if (appId.isBlank()) throw new IllegalStateException("启用飞书机器人时必须配置 FEISHU_APP_ID。");
    if (appSecret.isBlank()) {
      throw new IllegalStateException("启用飞书机器人时必须配置 FEISHU_APP_SECRET。");
    }
    if (taskDefinitionId.isBlank()) {
      throw new IllegalStateException("启用飞书机器人时必须配置 FEISHU_TASK_DEFINITION_ID。");
    }
    if (appId.length() > 128) {
      throw new IllegalStateException("FEISHU_APP_ID 长度不能超过 128 个字符。");
    }
    try {
      UUID.fromString(taskDefinitionId);
    } catch (IllegalArgumentException error) {
      throw new IllegalStateException("FEISHU_TASK_DEFINITION_ID 必须是有效 UUID。", error);
    }
    if (eventPollIntervalMs < 250 || eventPollIntervalMs > 60_000) {
      throw new IllegalStateException("FEISHU_EVENT_POLL_INTERVAL_MS 必须在 250 到 60000 之间。");
    }
  }
}
