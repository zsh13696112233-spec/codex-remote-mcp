package com.codexflow.configcenter.domain;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

/** 任务定义的 Spring Data JPA 数据访问接口。 */
interface TaskDefinitionRepository extends JpaRepository<TaskDefinitionEntity, String> {

  /** 查询未软删除且名称匹配的任务定义，并按创建时间倒序返回。 */
  List<TaskDefinitionEntity> findByDeletedFalseAndNameContainingIgnoreCaseOrderByCreatedAtDesc(
      String query);

  /** 判断指定 SOP 是否仍被任一未软删除任务引用。 */
  boolean existsBySopIdAndDeletedFalse(String sopId);
}
