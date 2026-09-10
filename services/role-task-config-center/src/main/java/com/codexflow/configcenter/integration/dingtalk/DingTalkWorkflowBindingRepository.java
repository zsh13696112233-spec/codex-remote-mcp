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

  @Query(
      """
      SELECT binding FROM DingTalkWorkflowBindingEntity binding
      WHERE binding.clientId = :clientId
        AND (binding.status IN ('submitting', 'active')
             OR (binding.status = 'terminal' AND (binding.waitingAssistant = true
                 OR EXISTS (SELECT card.id FROM DingTalkOutboxEntity card
                     WHERE card.workflowId = binding.workflowId AND card.messageKind = 'waiting_card'
                       AND card.deliveredAt IS NOT NULL AND card.waitingCardState IN ('countdown', 'held')))))
      ORDER BY binding.createdAt
      """)
  List<DingTalkWorkflowBindingEntity> findPollable(@Param("clientId") String clientId);

  List<DingTalkWorkflowBindingEntity> findByClientIdAndWaitingAssistantTrueOrderByUpdatedAt(
      String clientId);
}
