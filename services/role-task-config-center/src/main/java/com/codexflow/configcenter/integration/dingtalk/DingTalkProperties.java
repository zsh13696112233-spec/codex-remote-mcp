package com.codexflow.configcenter.integration.dingtalk;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/** 钉钉机器人长连接配置；任务路由由任务定义与通知对象的绑定决定。 */
@Component
@ConfigurationProperties(prefix = "dingtalk")
public class DingTalkProperties {

  private boolean enabled;
  private String clientId = "";
  private String clientSecret = "";
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
    if (clientId.length() > 128) {
      throw new IllegalStateException("DINGTALK_CLIENT_ID 长度不能超过 128 个字符。");
    }
    if (cardTemplateId.length() > 256) {
      throw new IllegalStateException("DINGTALK_CARD_TEMPLATE_ID 不能超过 256 个字符。");
    }
    if (eventPollIntervalMs < 250 || eventPollIntervalMs > 60_000) {
      throw new IllegalStateException("DINGTALK_EVENT_POLL_INTERVAL_MS 必须在 250 到 60000 之间。");
    }
  }
}
