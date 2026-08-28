package com.codexflow.configcenter.integration.dingtalk;

import java.time.Instant;
import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

interface DingTalkOutboxRepository extends JpaRepository<DingTalkOutboxEntity, String> {

  boolean existsByDedupKey(String dedupKey);

  Optional<DingTalkOutboxEntity> findFirstByConversationIdAndSentMessageIdOrderByCreatedAtDesc(
      String conversationId, String sentMessageId);

  @Query(
      value =
          """
          SELECT *
          FROM codex_sop_dingtalk_outbox
          WHERE workflow_id = :workflowId AND message_kind IN ('card', 'card_update')
          ORDER BY created_at DESC, id DESC
          LIMIT 1
          """,
      nativeQuery = true)
  Optional<DingTalkOutboxEntity> findLatestCard(@Param("workflowId") String workflowId);

  @Query(
      value =
          """
          SELECT *
          FROM codex_sop_dingtalk_outbox
          WHERE status IN (:statuses) AND next_attempt_at <= :nextAttemptAt
          ORDER BY created_at
          LIMIT 50
          FOR UPDATE
          """,
      nativeQuery = true)
  List<DingTalkOutboxEntity> findDueForUpdate(
      @Param("statuses") List<String> statuses, @Param("nextAttemptAt") Instant nextAttemptAt);
}
