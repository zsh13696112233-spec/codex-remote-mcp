package com.codexflow.configcenter.domain;

import jakarta.persistence.LockModeType;
import java.time.LocalDate;
import java.time.LocalTime;
import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

/** 任务定义的 Spring Data JPA 数据访问接口。 */
interface TaskDefinitionRepository extends JpaRepository<TaskDefinitionEntity, String> {

  /** 查询未软删除且名称匹配的任务定义，并按创建时间倒序返回。 */
  List<TaskDefinitionEntity> findByDeletedFalseAndNameContainingIgnoreCaseOrderByCreatedAtDesc(
      String query);

  /** 判断指定 SOP 是否仍被任一未软删除任务引用。 */
  boolean existsBySopIdAndDeletedFalse(String sopId);

  boolean existsByDingtalkTargetIdAndDeletedFalse(String targetId);

  Optional<TaskDefinitionEntity> findFirstByDingtalkTargetIdAndDeletedFalse(String targetId);

  @Query(
      value =
          "SELECT task.* FROM codex_sop_task_definitions task JOIN codex_sop_dingtalk_targets"
              + " target ON target.id = task.dingtalk_target_id WHERE target.client_id = :clientId"
              + " AND target.target_type = :targetType AND target.external_id = :externalId AND"
              + " task.deleted = FALSE FOR UPDATE",
      nativeQuery = true)
  Optional<TaskDefinitionEntity> findDingTalkBindingForUpdate(
      @Param("clientId") String clientId,
      @Param("targetType") String targetType,
      @Param("externalId") String externalId);

  @Query(
      "SELECT task FROM TaskDefinitionEntity task WHERE task.deleted = false AND"
          + " task.dingtalkTarget.clientId = :clientId AND task.dingtalkTarget.targetType ="
          + " :targetType AND task.dingtalkTarget.externalId = :externalId")
  Optional<TaskDefinitionEntity> findDingTalkBinding(
      @Param("clientId") String clientId,
      @Param("targetType") String targetType,
      @Param("externalId") String externalId);

  @Query(
      value = "SELECT * FROM codex_sop_task_definitions WHERE id = :id FOR UPDATE",
      nativeQuery = true)
  Optional<TaskDefinitionEntity> findForUpdate(@Param("id") String id);

  List<TaskDefinitionEntity> findByActiveWorkflowIdIsNotNull();

  @Modifying(flushAutomatically = true)
  @Query(
      value =
          "UPDATE codex_sop_task_definitions SET active_workflow_id = CASE WHEN active_workflow_id"
              + " = :workflowId THEN NULL ELSE active_workflow_id END, dingtalk_active_workflow_id"
              + " = CASE WHEN dingtalk_active_workflow_id = :workflowId THEN NULL ELSE"
              + " dingtalk_active_workflow_id END, updated_at = CURRENT_TIMESTAMP(6) WHERE"
              + " active_workflow_id = :workflowId OR dingtalk_active_workflow_id = :workflowId",
      nativeQuery = true)
  int releaseWorkflow(@Param("workflowId") String workflowId);

  @Lock(LockModeType.PESSIMISTIC_WRITE)
  @Query(
      "SELECT task FROM TaskDefinitionEntity task WHERE task.deleted = false AND task.enabled ="
          + " true AND task.scheduleEnabled = true AND task.scheduleTime = :scheduleTime AND"
          + " (task.lastScheduleDate IS NULL OR task.lastScheduleDate <> :scheduleDate)")
  List<TaskDefinitionEntity> findDueSchedulesForUpdate(
      @Param("scheduleDate") LocalDate scheduleDate, @Param("scheduleTime") LocalTime scheduleTime);

  @Query(
      "SELECT COUNT(task) > 0 FROM TaskDefinitionEntity task WHERE task.deleted = false AND"
          + " task.dingtalkTarget.clientId = :clientId AND task.dingtalkActiveWorkflowId IS NOT"
          + " NULL")
  boolean existsActiveDingTalkBinding(@Param("clientId") String clientId);
}
