package com.codexflow.configcenter.domain;

import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

interface DingTalkTargetRepository extends JpaRepository<DingTalkTargetEntity, String> {

  List<DingTalkTargetEntity> findByClientIdAndDeletedFalseOrderByTargetTypeAscDisplayNameAsc(
      String clientId);

  Optional<DingTalkTargetEntity> findByClientIdAndTargetTypeAndExternalId(
      String clientId, String targetType, String externalId);
}
