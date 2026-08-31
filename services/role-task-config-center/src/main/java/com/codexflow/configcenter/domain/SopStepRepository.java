package com.codexflow.configcenter.domain;

import org.springframework.data.jpa.repository.JpaRepository;

/** SOP 步骤的 Spring Data JPA 数据访问接口。 */
interface SopStepRepository extends JpaRepository<SopStepEntity, String> {

  /** 判断指定角色是否仍被任一未软删除 SOP 的步骤引用。 */
  boolean existsByRoleIdAndSopDeletedFalse(String roleId);
}
