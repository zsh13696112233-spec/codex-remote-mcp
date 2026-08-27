package com.codexflow.configcenter.integration.feishu;

import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

interface FeishuWorkflowBindingRepository
    extends JpaRepository<FeishuWorkflowBindingEntity, String> {

  @Query(
      value = "SELECT * FROM codex_sop_feishu_workflow_bindings WHERE workflow_id = :id FOR UPDATE",
      nativeQuery = true)
  Optional<FeishuWorkflowBindingEntity> findForUpdate(@Param("id") String workflowId);

  Optional<FeishuWorkflowBindingEntity> findByAppIdAndTriggerMessageId(
      String appId, String triggerMessageId);

  Optional<FeishuWorkflowBindingEntity> findFirstByAppIdAndChatIdAndThreadIdOrderByCreatedAtDesc(
      String appId, String chatId, String threadId);

  Optional<FeishuWorkflowBindingEntity>
      findFirstByAppIdAndChatIdAndRootMessageIdOrderByCreatedAtDesc(
          String appId, String chatId, String rootMessageId);

  List<FeishuWorkflowBindingEntity> findByAppIdAndStatusInOrderByCreatedAt(
      String appId, List<String> statuses);

  List<FeishuWorkflowBindingEntity> findByAppIdAndWaitingAssistantTrueOrderByUpdatedAt(
      String appId);
}
