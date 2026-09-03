package com.codexflow.configcenter.domain;

import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

interface DingTalkDepartmentRepository extends JpaRepository<DingTalkDepartmentEntity, String> {

  List<DingTalkDepartmentEntity> findByClientIdOrderByDisplayNameAsc(String clientId);

  Optional<DingTalkDepartmentEntity> findByClientIdAndExternalId(
      String clientId, String externalId);
}
