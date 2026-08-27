package com.codexflow.configcenter.integration.dingtalk;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;

/** 钉钉消息与任务助手幂等消息编号的映射。 */
@Entity
@Table(name = "codex_sop_dingtalk_inbound_messages")
class DingTalkInboundMessageEntity {

  @Id
  @Column(name = "message_id", length = 256)
  String messageId;

  @Column(name = "workflow_id", nullable = false, length = 128)
  String workflowId;

  @Column(name = "workflow_message_id", nullable = false, length = 36)
  String workflowMessageId;

  @Column(name = "sender_user_id", nullable = false, length = 256)
  String senderUserId;

  @Column(nullable = false, length = 32)
  String status;

  @Column(name = "created_at", nullable = false)
  Instant createdAt;

  @Column(name = "updated_at", nullable = false)
  Instant updatedAt;
}
