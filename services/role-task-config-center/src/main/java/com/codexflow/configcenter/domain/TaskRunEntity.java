package com.codexflow.configcenter.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.FetchType;
import jakarta.persistence.Id;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;
import jakarta.persistence.Table;
import java.time.Instant;

/** 一次工作流运行的持久化记录，对应数据库表 {@code codex_sop_task_runs}。 */
@Entity
@Table(name = "codex_sop_task_runs")
class TaskRunEntity {

  /** 工作流 ID，同时作为运行记录主键。 */
  @Id
  @Column(name = "workflow_id")
  String workflowId;

  /** 是否已向运行时补齐该运行的任务归属。 */
  @Column(name = "runtime_scope_registered", nullable = false)
  boolean runtimeScopeRegistered;

  /** 本次运行来源的任务定义；历史查询需要任务信息，因此采用立即加载。 */
  @ManyToOne(fetch = FetchType.EAGER, optional = false)
  @JoinColumn(name = "task_definition_id")
  TaskDefinitionEntity taskDefinition;

  /** 重试运行对应的源工作流 ID；首次运行时为空。 */
  @Column(name = "source_workflow_id")
  String sourceWorkflowId;

  /** 最近一次已知的提交或执行状态。 */
  @Column(nullable = false)
  String status;

  /** 提交时冻结的任务和 SOP 快照 JSON，使用 LONGTEXT 保存。 */
  @Column(name = "snapshot_json", nullable = false, columnDefinition = "LONGTEXT")
  String snapshotJson;

  /** 发送给工作流网关的完整请求 JSON。 */
  @Column(name = "submitted_json", nullable = false, columnDefinition = "LONGTEXT")
  String submittedJson;

  /** 工作流网关最近一次成功响应的 JSON。 */
  @Column(name = "gateway_response_json", columnDefinition = "LONGTEXT")
  String gatewayResponseJson;

  /** 提交失败时记录的错误信息。 */
  @Column(name = "error_message", columnDefinition = "LONGTEXT")
  String errorMessage;

  /** 运行记录首次提交时间。 */
  @Column(name = "submitted_at", nullable = false)
  Instant submittedAt;

  /** 运行状态最近一次持久化更新时间。 */
  @Column(name = "updated_at", nullable = false)
  Instant updatedAt;
}
