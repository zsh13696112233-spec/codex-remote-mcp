package com.codexflow.configcenter.integration.dingtalk;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;

/** 等待可靠发送到钉钉的文本或卡片消息。 */
@Entity
@Table(name = "codex_sop_dingtalk_outbox")
class DingTalkOutboxEntity {

  @Id
  @Column(length = 36)
  String id;

  @Column(name = "dedup_key", nullable = false, unique = true, length = 512)
  String dedupKey;

  @Column(name = "workflow_id", length = 128)
  String workflowId;

  @Column(name = "conversation_id", nullable = false, length = 256)
  String conversationId;

  @Column(name = "reply_to_message_id", length = 256)
  String replyToMessageId;

  @Column(name = "message_kind", nullable = false, length = 32)
  String messageKind;

  @Column(name = "payload_json", nullable = false, columnDefinition = "LONGTEXT")
  String payloadJson;

  @Column(nullable = false, length = 32)
  String status;

  @Column(name = "attempt_count", nullable = false)
  int attemptCount;

  @Column(name = "next_attempt_at", nullable = false)
  Instant nextAttemptAt;

  @Column(name = "last_error", length = 2000)
  String lastError;

  @Column(name = "sent_message_id", length = 256)
  String sentMessageId;

  @Column(name = "created_at", nullable = false)
  Instant createdAt;

  @Column(name = "updated_at", nullable = false)
  Instant updatedAt;
}
