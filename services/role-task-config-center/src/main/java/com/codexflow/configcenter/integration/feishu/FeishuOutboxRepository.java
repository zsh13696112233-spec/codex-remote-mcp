package com.codexflow.configcenter.integration.feishu;

import java.time.Instant;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

interface FeishuOutboxRepository extends JpaRepository<FeishuOutboxEntity, String> {

  boolean existsByDedupKey(String dedupKey);

  @Query(
      value =
          """
          SELECT *
          FROM codex_sop_feishu_outbox
          WHERE status IN (:statuses) AND next_attempt_at <= :nextAttemptAt
          ORDER BY created_at
          LIMIT 50
          FOR UPDATE
          """,
      nativeQuery = true)
  List<FeishuOutboxEntity> findDueForUpdate(
      @Param("statuses") List<String> statuses, @Param("nextAttemptAt") Instant nextAttemptAt);
}
