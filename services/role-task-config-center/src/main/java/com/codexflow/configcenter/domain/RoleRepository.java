package com.codexflow.configcenter.domain;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

/** 角色实体的 Spring Data JPA 数据访问接口。 */
interface RoleRepository extends JpaRepository<RoleEntity, String> {

  /** 按名称模糊查询未软删除的角色，并将新建角色排在前面。 */
  List<RoleEntity> findByDeletedFalseAndNameContainingIgnoreCaseOrderByCreatedAtDesc(String query);

  /** 判断除指定 ID 外是否已有同名角色，用于创建和更新前的唯一性检查。 */
  boolean existsByNameIgnoreCaseAndIdNot(String name, String id);
}
