package com.codexflow.configcenter.integration.feishu;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;

/** 飞书任务话题与一个工作流的持久化绑定。 */
@Entity
@Table(name = "codex_sop_feishu_workflow_bindings")
class FeishuWorkflowBindingEntity {

  @Id
  @Column(name = "workflow_id", length = 128)
  String workflowId;

  @Column(name = "app_id", nullable = false, length = 128)
  String appId;

  @Column(name = "task_definition_id", nullable = false, length = 36)
  String taskDefinitionId;

  @Column(name = "trigger_message_id", nullable = false, length = 256)
  String triggerMessageId;

  @Column(name = "chat_id", nullable = false, length = 256)
  String chatId;

  @Column(name = "root_message_id", nullable = false, length = 256)
  String rootMessageId;

  @Column(name = "thread_id", length = 256)
  String threadId;

  @Column(name = "initiator_open_id", nullable = false, length = 256)
  String initiatorOpenId;

  @Column(nullable = false, length = 32)
  String status;

  @Column(name = "event_cursor", nullable = false)
  long eventCursor;

  @Column(name = "progress_message_id", length = 256)
  String progressMessageId;

  @Column(name = "waiting_assistant", nullable = false)
  boolean waitingAssistant;

  @Column(name = "created_at", nullable = false)
  Instant createdAt;

  @Column(name = "updated_at", nullable = false)
  Instant updatedAt;
}
