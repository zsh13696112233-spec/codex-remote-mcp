package com.codexflow.configcenter.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.FetchType;
import jakarta.persistence.Id;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;
import jakarta.persistence.Table;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalTime;

/** 可重复运行的任务定义实体，对应数据库表 {@code codex_sop_task_definitions}。 */
@Entity
@Table(name = "codex_sop_task_definitions")
class TaskDefinitionEntity extends Timestamped {

  /** 任务定义主键，使用应用生成的 UUID。 */
  @Id String id;

  /** 任务名称，数据库中不允许为空。 */
  @Column(nullable = false)
  String name;

  /** 任务目标正文，使用 MySQL LONGTEXT 保存。 */
  @Column(nullable = false, columnDefinition = "LONGTEXT")
  String objective;

  /** 任务采用的 SOP；生成运行快照时需要完整 SOP，因此采用立即加载。 */
  @ManyToOne(fetch = FetchType.EAGER, optional = false)
  @JoinColumn(name = "sop_id")
  SopEntity sop;

  /** 提交任务时附加到目标后的可选补充说明。 */
  @Column(name = "additional_notes", columnDefinition = "LONGTEXT")
  String additionalNotes;

  /** 从钉钉启动该任务时使用的唯一人员或群聊；同一对象只能绑定一个任务定义。 */
  @ManyToOne
  @JoinColumn(name = "dingtalk_target_id")
  DingTalkTargetEntity dingtalkTarget;

  /** 该钉钉任务绑定当前占用的工作流；不同任务定义之间可以并行。 */
  @Column(name = "dingtalk_active_workflow_id", length = 128)
  String dingtalkActiveWorkflowId;

  /** 是否启用服务端定时运行。 */
  @Column(name = "schedule_enabled", nullable = false)
  boolean scheduleEnabled;

  /** 每日运行时间，仅保存小时和分钟。 */
  @Column(name = "schedule_time")
  LocalTime scheduleTime;

  /** 定时模式：每天固定时间或按分钟间隔执行。 */
  @Column(name = "schedule_mode", nullable = false, length = 16)
  String scheduleMode = "daily";

  /** 间隔模式的分钟数，允许范围为 5–1440。 */
  @Column(name = "schedule_interval_minutes")
  Integer scheduleIntervalMinutes;

  /** 间隔模式下一次计划触发的绝对时间。 */
  @Column(name = "next_interval_at")
  Instant nextIntervalAt;

  /** 网页和定时运行是否主动推送到任务绑定的钉钉对象。 */
  @Column(name = "notify_dingtalk", nullable = false)
  boolean notifyDingTalk;

  /** 最近一次已经处理的定时日期，用于避免同一分钟重复触发。 */
  @Column(name = "last_schedule_date")
  LocalDate lastScheduleDate;

  /** 当前占用该任务定义的工作流；所有启动来源共用一个运行槽。 */
  @Column(name = "active_workflow_id", length = 128)
  String activeWorkflowId;

  /** 任务定义是否允许发起新的运行。 */
  @Column(nullable = false)
  boolean enabled = true;

  /** 软删除标记；删除后的任务不出现在配置列表中，但历史运行仍可引用。 */
  @Column(nullable = false)
  boolean deleted = false;
}
