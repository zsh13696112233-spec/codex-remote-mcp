package com.codexflow.configcenter.domain;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

/** 任务运行记录的 Spring Data JPA 数据访问接口。 */
interface TaskRunRepository extends JpaRepository<TaskRunEntity, String> {

  /** 查询指定任务定义的全部运行记录，并按提交时间倒序返回。 */
  List<TaskRunEntity> findByTaskDefinitionIdOrderBySubmittedAtDesc(String taskDefinitionId);
}
