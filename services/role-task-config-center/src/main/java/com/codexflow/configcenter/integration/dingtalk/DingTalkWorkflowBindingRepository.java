package com.codexflow.configcenter.integration.dingtalk;

import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

interface DingTalkWorkflowBindingRepository
    extends JpaRepository<DingTalkWorkflowBindingEntity, String> {

  @Query(
      value =
          "SELECT * FROM codex_sop_dingtalk_workflow_bindings WHERE workflow_id = :id FOR UPDATE",
      nativeQuery = true)
  Optional<DingTalkWorkflowBindingEntity> findForUpdate(@Param("id") String workflowId);

  Optional<DingTalkWorkflowBindingEntity> findByClientIdAndTriggerMessageId(
      String clientId, String triggerMessageId);

  Optional<DingTalkWorkflowBindingEntity>
      findFirstByClientIdAndConversationIdAndRootMessageIdOrderByCreatedAtDesc(
          String clientId, String conversationId, String rootMessageId);

  Optional<DingTalkWorkflowBindingEntity>
      findFirstByClientIdAndConversationIdAndProgressCardInstanceIdOrderByCreatedAtDesc(
          String clientId, String conversationId, String progressCardInstanceId);

  List<DingTalkWorkflowBindingEntity> findByClientIdAndStatusInOrderByCreatedAt(
      String clientId, List<String> statuses);

  List<DingTalkWorkflowBindingEntity> findByClientIdAndWaitingAssistantTrueOrderByUpdatedAt(
      String clientId);
}
