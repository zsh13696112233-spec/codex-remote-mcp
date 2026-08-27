package com.codexflow.configcenter.integration.dingtalk;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.time.Instant;

/** 单个钉钉应用的全局单任务占用状态。 */
@Entity
@Table(name = "codex_sop_dingtalk_bot_state")
class DingTalkBotStateEntity {

  @Id
  @Column(name = "client_id", length = 128)
  String clientId;

  @Column(name = "active_workflow_id", length = 128)
  String activeWorkflowId;

  @Version
  @Column(nullable = false)
  long version;

  @Column(name = "updated_at", nullable = false)
  Instant updatedAt;
}
