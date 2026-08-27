package com.codexflow.configcenter.integration.dingtalk;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;

/** 钉钉群消息回复链与一个工作流的持久化绑定。 */
@Entity
@Table(name = "codex_sop_dingtalk_workflow_bindings")
class DingTalkWorkflowBindingEntity {

  @Id
  @Column(name = "workflow_id", length = 128)
  String workflowId;

  @Column(name = "client_id", nullable = false, length = 128)
  String clientId;

  @Column(name = "task_definition_id", nullable = false, length = 36)
  String taskDefinitionId;

  @Column(name = "trigger_message_id", nullable = false, length = 256)
  String triggerMessageId;

  @Column(name = "conversation_id", nullable = false, length = 256)
  String conversationId;

  @Column(name = "root_message_id", nullable = false, length = 256)
  String rootMessageId;

  @Column(name = "initiator_user_id", nullable = false, length = 256)
  String initiatorUserId;

  @Column(nullable = false, length = 32)
  String status;

  @Column(name = "event_cursor", nullable = false)
  long eventCursor;

  @Column(name = "progress_card_instance_id", length = 256)
  String progressCardInstanceId;

  @Column(name = "waiting_assistant", nullable = false)
  boolean waitingAssistant;

  @Column(name = "created_at", nullable = false)
  Instant createdAt;

  @Column(name = "updated_at", nullable = false)
  Instant updatedAt;
}
