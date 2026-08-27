package com.codexflow.configcenter.integration.feishu;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.time.Instant;

/** 单个飞书应用的全局单任务占用状态。 */
@Entity
@Table(name = "codex_sop_feishu_bot_state")
class FeishuBotStateEntity {

  @Id
  @Column(name = "app_id", length = 128)
  String appId;

  @Column(name = "active_workflow_id", length = 128)
  String activeWorkflowId;

  @Version
  @Column(nullable = false)
  long version;

  @Column(name = "updated_at", nullable = false)
  Instant updatedAt;
}
