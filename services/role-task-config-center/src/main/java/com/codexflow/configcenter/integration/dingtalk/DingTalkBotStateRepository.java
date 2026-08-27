package com.codexflow.configcenter.integration.dingtalk;

import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

interface DingTalkBotStateRepository extends JpaRepository<DingTalkBotStateEntity, String> {

  @Modifying
  @Query(
      value =
          "INSERT IGNORE INTO codex_sop_dingtalk_bot_state(client_id, active_workflow_id, version, updated_at) VALUES (:clientId, NULL, 0, CURRENT_TIMESTAMP(6))",
      nativeQuery = true)
  void insertIfAbsent(@Param("clientId") String clientId);

  @Query(
      value = "SELECT * FROM codex_sop_dingtalk_bot_state WHERE client_id = :clientId FOR UPDATE",
      nativeQuery = true)
  Optional<DingTalkBotStateEntity> findForUpdate(@Param("clientId") String clientId);
}
