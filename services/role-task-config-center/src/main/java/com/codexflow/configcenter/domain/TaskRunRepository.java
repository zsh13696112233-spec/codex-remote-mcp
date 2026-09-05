package com.codexflow.configcenter.domain;

import java.time.Instant;
import java.util.List;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

/** 任务运行记录的 Spring Data JPA 数据访问接口。 */
interface TaskRunRepository extends JpaRepository<TaskRunEntity, String> {

  /** 查询指定任务定义的全部运行记录，并按提交时间倒序返回。 */
  List<TaskRunEntity> findByTaskDefinitionIdOrderBySubmittedAtDesc(String taskDefinitionId);

  interface Summary {
    String getWorkflowId();

    String getSourceWorkflowId();

    String getStatus();

    Instant getSubmittedAt();

    Instant getUpdatedAt();
  }

  @Query(
      "SELECT r.workflowId AS workflowId, r.sourceWorkflowId AS sourceWorkflowId, "
          + "r.status AS status, r.submittedAt AS submittedAt, r.updatedAt AS updatedAt "
          + "FROM TaskRunEntity r WHERE r.taskDefinition.id = :taskId "
          + "ORDER BY r.submittedAt DESC, r.workflowId DESC")
  List<Summary> summaries(@Param("taskId") String taskId, Pageable pageable);

  @Query(
      "SELECT r.workflowId FROM TaskRunEntity r WHERE r.taskDefinition.id = :taskId "
          + "AND r.runtimeScopeRegistered = false ORDER BY r.workflowId")
  List<String> pendingScopes(@Param("taskId") String taskId, Pageable pageable);

  boolean existsByTaskDefinitionIdAndRuntimeScopeRegisteredFalseAndWorkflowIdNot(
      String taskDefinitionId, String workflowId);

  @Query(
      "SELECT DISTINCT r.taskDefinition.id FROM TaskRunEntity r WHERE r.runtimeScopeRegistered = false ORDER BY r.taskDefinition.id")
  List<String> pendingScopeTasks(Pageable pageable);

  @Modifying
  @Query("UPDATE TaskRunEntity r SET r.runtimeScopeRegistered = true WHERE r.workflowId IN :ids")
  void markScopes(@Param("ids") List<String> ids);

  @Modifying
  @Query(
      "UPDATE TaskRunEntity r SET r.status = :status, r.updatedAt = :now "
          + "WHERE r.workflowId = :id AND r.status <> :status")
  void updateStatus(
      @Param("id") String id, @Param("status") String status, @Param("now") Instant now);
}
