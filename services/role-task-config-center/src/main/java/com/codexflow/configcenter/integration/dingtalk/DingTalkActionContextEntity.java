package com.codexflow.configcenter.integration.dingtalk;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;

/** 按钮请求的人员快照；只有中央实际接受的关联才用于结果提醒。 */
@Entity
@Table(name = "codex_sop_dingtalk_action_contexts")
class DingTalkActionContextEntity {
  @Id
  @Column(length = 36)
  String id;

  @Column(name = "workflow_id", nullable = false, length = 128)
  String workflowId;

  @Column(name = "gate_id", nullable = false, length = 128)
  String gateId;

  @Column(name = "action_name", nullable = false, length = 32)
  String action;

  @Column(name = "operator_user_id", nullable = false, length = 256)
  String operatorUserId;

  @Column(name = "operator_display_name", nullable = false, length = 160)
  String operatorDisplayName;

  @Column(name = "created_at", nullable = false)
  Instant createdAt;
}
