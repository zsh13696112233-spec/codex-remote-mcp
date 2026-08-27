package com.codexflow.configcenter.integration.dingtalk;

import java.util.UUID;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/** 钉钉机器人长连接和固定任务绑定配置。 */
@Component
@ConfigurationProperties(prefix = "dingtalk")
public class DingTalkProperties {

  private boolean enabled;
  private String clientId = "";
  private String clientSecret = "";
  private String taskDefinitionId = "";
  private String cardTemplateId = "";
  private long eventPollIntervalMs = 1000;

  public boolean isEnabled() {
    return enabled;
  }

  public void setEnabled(boolean enabled) {
    this.enabled = enabled;
  }

  public String getClientId() {
    return clientId;
  }

  public void setClientId(String clientId) {
    this.clientId = clientId == null ? "" : clientId.trim();
  }

  public String getClientSecret() {
    return clientSecret;
  }

  public void setClientSecret(String clientSecret) {
    this.clientSecret = clientSecret == null ? "" : clientSecret.trim();
  }

  public String getTaskDefinitionId() {
    return taskDefinitionId;
  }

  public void setTaskDefinitionId(String taskDefinitionId) {
    this.taskDefinitionId = taskDefinitionId == null ? "" : taskDefinitionId.trim();
  }

  public String getCardTemplateId() {
    return cardTemplateId;
  }

  public void setCardTemplateId(String cardTemplateId) {
    this.cardTemplateId = cardTemplateId == null ? "" : cardTemplateId.trim();
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
    if (clientId.isBlank()) {
      throw new IllegalStateException("启用钉钉机器人时必须配置 DINGTALK_CLIENT_ID。");
    }
    if (clientSecret.isBlank()) {
      throw new IllegalStateException("启用钉钉机器人时必须配置 DINGTALK_CLIENT_SECRET。");
    }
    if (taskDefinitionId.isBlank()) {
      throw new IllegalStateException("启用钉钉机器人时必须配置 DINGTALK_TASK_DEFINITION_ID。");
    }
    if (clientId.length() > 128) {
      throw new IllegalStateException("DINGTALK_CLIENT_ID 长度不能超过 128 个字符。");
    }
    if (cardTemplateId.isBlank() || cardTemplateId.length() > 256) {
      throw new IllegalStateException("DINGTALK_CARD_TEMPLATE_ID 必须填写且不能超过 256 个字符。");
    }
    try {
      UUID.fromString(taskDefinitionId);
    } catch (IllegalArgumentException error) {
      throw new IllegalStateException("DINGTALK_TASK_DEFINITION_ID 必须是有效 UUID。", error);
    }
    if (eventPollIntervalMs < 250 || eventPollIntervalMs > 60_000) {
      throw new IllegalStateException("DINGTALK_EVENT_POLL_INTERVAL_MS 必须在 250 到 60000 之间。");
    }
  }
}
