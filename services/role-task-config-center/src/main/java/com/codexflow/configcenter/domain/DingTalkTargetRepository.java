package com.codexflow.configcenter.domain;

import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

interface DingTalkTargetRepository extends JpaRepository<DingTalkTargetEntity, String> {

  List<DingTalkTargetEntity> findByClientIdAndDeletedFalseOrderByTargetTypeAscDisplayNameAsc(
      String clientId);

  Optional<DingTalkTargetEntity> findByClientIdAndTargetTypeAndExternalId(
      String clientId, String targetType, String externalId);

  @Query(
      value = "SELECT * FROM codex_sop_dingtalk_targets WHERE id = :id FOR UPDATE",
      nativeQuery = true)
  Optional<DingTalkTargetEntity> findForUpdate(@Param("id") String id);
}
