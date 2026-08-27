package com.codexflow.configcenter.integration.feishu;

import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

interface FeishuBotStateRepository extends JpaRepository<FeishuBotStateEntity, String> {

  @Modifying
  @Query(
      value =
          "INSERT IGNORE INTO codex_sop_feishu_bot_state(app_id, active_workflow_id, version, updated_at) VALUES (:appId, NULL, 0, CURRENT_TIMESTAMP(6))",
      nativeQuery = true)
  void insertIfAbsent(@Param("appId") String appId);

  @Query(
      value = "SELECT * FROM codex_sop_feishu_bot_state WHERE app_id = :appId FOR UPDATE",
      nativeQuery = true)
  Optional<FeishuBotStateEntity> findForUpdate(@Param("appId") String appId);
}
