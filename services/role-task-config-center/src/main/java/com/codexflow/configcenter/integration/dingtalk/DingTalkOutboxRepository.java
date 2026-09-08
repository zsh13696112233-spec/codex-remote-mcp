package com.codexflow.configcenter.integration.dingtalk;

import java.time.Instant;
import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

interface DingTalkOutboxRepository extends JpaRepository<DingTalkOutboxEntity, String> {

  List<DingTalkOutboxEntity> findTop50ByConversationIdAndStatusOrderByCreatedAtDesc(
      String conversationId, String status);

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
          SELECT candidate.*
          FROM codex_sop_dingtalk_outbox candidate
          WHERE candidate.status IN (:statuses) AND candidate.next_attempt_at <= :nextAttemptAt
            AND NOT EXISTS (
              SELECT 1 FROM codex_sop_dingtalk_outbox earlier
              WHERE (earlier.workflow_id = candidate.workflow_id
                     OR (earlier.workflow_id IS NULL AND candidate.workflow_id IS NULL))
                AND earlier.conversation_id = candidate.conversation_id
                AND earlier.delivery_order < candidate.delivery_order
                AND earlier.status IN ('pending', 'failed', 'sending')
            )
          ORDER BY candidate.delivery_order
          LIMIT :limit
          FOR UPDATE
          """,
      nativeQuery = true)
  List<DingTalkOutboxEntity> findDueForUpdate(
      @Param("statuses") List<String> statuses,
      @Param("nextAttemptAt") Instant nextAttemptAt,
      @Param("limit") int limit);
}
