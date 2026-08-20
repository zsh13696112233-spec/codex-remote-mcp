package com.codexflow.configcenter.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.FetchType;
import jakarta.persistence.Id;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;
import jakarta.persistence.Table;

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

  /** 任务定义是否允许发起新的运行。 */
  @Column(nullable = false)
  boolean enabled = true;

  /** 软删除标记；删除后的任务不出现在配置列表中，但历史运行仍可引用。 */
  @Column(nullable = false)
  boolean deleted = false;
}
