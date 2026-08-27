package com.codexflow.configcenter.integration.dingtalk;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.time.Instant;

/** 页面维护的单机器人运行配置。 */
@Entity
@Table(name = "codex_sop_dingtalk_bot_settings")
class DingTalkSettingsEntity {

  @Id Byte id;

  @Column(nullable = false)
  boolean enabled;

  @Column(name = "client_id", nullable = false, length = 128)
  String clientId;

  @Column(name = "client_secret", nullable = false, length = 512)
  String clientSecret;

  @Column(name = "task_definition_id", nullable = false, length = 36)
  String taskDefinitionId;

  @Column(name = "card_template_id", nullable = false, length = 256)
  String cardTemplateId;

  @Column(name = "event_poll_interval_ms", nullable = false)
  long eventPollIntervalMs;

  @Version
  @Column(nullable = false)
  long version;

  @Column(name = "updated_at", nullable = false)
  Instant updatedAt;
}
