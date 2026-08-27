package com.codexflow.configcenter.integration.feishu;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.time.Instant;

/** 页面维护的单机器人运行配置。 */
@Entity
@Table(name = "codex_sop_feishu_bot_settings")
class FeishuSettingsEntity {

  @Id Byte id;

  @Column(nullable = false)
  boolean enabled;

  @Column(name = "app_id", nullable = false, length = 128)
  String appId;

  @Column(name = "app_secret", nullable = false, length = 512)
  String appSecret;

  @Column(name = "task_definition_id", nullable = false, length = 36)
  String taskDefinitionId;

  @Column(name = "event_poll_interval_ms", nullable = false)
  long eventPollIntervalMs;

  @Version
  @Column(nullable = false)
  long version;

  @Column(name = "updated_at", nullable = false)
  Instant updatedAt;
}
