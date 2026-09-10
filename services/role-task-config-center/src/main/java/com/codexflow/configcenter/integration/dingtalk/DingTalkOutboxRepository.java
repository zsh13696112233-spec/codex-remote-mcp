package com.codexflow.configcenter.integration.dingtalk;

import java.time.Instant;
import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

interface DingTalkOutboxRepository extends JpaRepository<DingTalkOutboxEntity, String> {

  @Query(
      "select count(o) from DingTalkOutboxEntity o where o.workflowId = :workflowId "
          + "and o.messageKind = 'waiting_card' and o.deliveredAt is not null "
          + "and ((o.targetType = 'GROUP' and :conversationType = '2' and o.conversationId = :conversationId) "
          + "or (o.targetType = 'PERSON' and :conversationType = '1' and o.targetExternalId = :senderId))")
  long countDeliveredQuotedCards(
      @Param("workflowId") String workflowId,
      @Param("conversationType") String conversationType,
      @Param("conversationId") String conversationId,
      @Param("senderId") String senderId);

  List<DingTalkOutboxEntity> findByWorkflowIdAndWaitingCardStateIn(
      String workflowId, List<String> states);

  @Query(
      value =
          "SELECT * FROM codex_sop_dingtalk_outbox WHERE conversation_id = :conversationId "
              + "AND ((status = 'sent' AND advance_gate_id IS NULL) OR delivered_at IS NOT NULL) "
              + "ORDER BY created_at DESC LIMIT 50",
      nativeQuery = true)
  List<DingTalkOutboxEntity> findRecentDelivered(@Param("conversationId") String conversationId);

  @Query(
      value =
          "SELECT * FROM codex_sop_dingtalk_outbox WHERE conversation_id = :conversationId "
              + "AND workflow_id = :workflowId "
              + "AND ((status = 'sent' AND advance_gate_id IS NULL) OR delivered_at IS NOT NULL) "
              + "ORDER BY created_at DESC, id DESC",
      nativeQuery = true)
  List<DingTalkOutboxEntity> findDeliveredForQuote(
      @Param("conversationId") String conversationId,
      @Param("workflowId") String workflowId,
      org.springframework.data.domain.Pageable pageable);

  List<DingTalkOutboxEntity> findTop50ByConversationIdAndStatusOrderByCreatedAtDesc(
      String conversationId, String status);

  boolean existsByDedupKey(String dedupKey);

  Optional<DingTalkOutboxEntity> findByDedupKey(String dedupKey);

  @Query(
      value =
          "SELECT COUNT(DISTINCT advance_gate_id) FROM codex_sop_dingtalk_outbox "
              + "WHERE workflow_id = :workflowId",
      nativeQuery = true)
  long countAdvanceNotices(@Param("workflowId") String workflowId);

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
