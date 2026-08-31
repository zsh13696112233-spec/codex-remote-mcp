package com.codexflow.configcenter.domain;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

/** SOP 聚合根的 Spring Data JPA 数据访问接口。 */
interface SopRepository extends JpaRepository<SopEntity, String> {

  /** 按名称模糊查询未软删除的 SOP，并将新建 SOP 排在前面。 */
  List<SopEntity> findByDeletedFalseAndNameContainingIgnoreCaseOrderByCreatedAtDesc(String query);
}
