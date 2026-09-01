package com.codexflow.configcenter.domain;

import jakarta.persistence.CollectionTable;
import jakarta.persistence.Column;
import jakarta.persistence.ElementCollection;
import jakarta.persistence.Entity;
import jakarta.persistence.FetchType;
import jakarta.persistence.Id;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;
import jakarta.persistence.Table;
import java.util.LinkedHashSet;
import java.util.Set;

/** SOP 步骤实体，对应数据库表 {@code codex_sop_steps}。 */
@Entity
@Table(name = "codex_sop_steps")
class SopStepEntity {

  /** 步骤主键，使用应用生成的 UUID。 */
  @Id String id;

  /** 所属 SOP；步骤写入时必须存在，使用延迟加载避免无条件加载整个聚合。 */
  @ManyToOne(fetch = FetchType.LAZY, optional = false)
  @JoinColumn(name = "sop_id")
  SopEntity sop;

  /** 步骤在 SOP 中的零基顺序号。 */
  @Column(name = "position_no", nullable = false)
  int positionNo;

  /** 面向用户展示的步骤名称。 */
  @Column(name = "display_name", nullable = false)
  String displayName;

  /** 步骤引用的执行角色。构建不可变运行快照时需要立即读取角色名称和职责，因此采用立即加载， 避免脱离事务后访问未初始化代理。 */
  @ManyToOne(fetch = FetchType.EAGER, optional = false)
  @JoinColumn(name = "role_id")
  RoleEntity role;

  /** 发送给步骤执行器的业务指令，使用 MySQL LONGTEXT 保存。 */
  @Column(name = "instruction_text", nullable = false, columnDefinition = "LONGTEXT")
  String instruction;

  /** 对步骤输出内容的预期说明，使用 MySQL LONGTEXT 保存。 */
  @Column(name = "expected_output", nullable = false, columnDefinition = "LONGTEXT")
  String expectedOutput;

  /** 执行器类型，当前允许 {@code local} 或 {@code remote}。 */
  @Column(name = "executor_type", nullable = false)
  String executorType;

  /** 实际执行该步骤的执行机标识。 */
  @Column(name = "agent_id", nullable = false)
  String agentId;

  /** 执行步骤时使用的可选工作目录。 */
  @Column(name = "working_directory")
  String workingDirectory;

  /** 是否允许步骤执行器写入工作目录。 */
  @Column(name = "write_enabled", nullable = false)
  boolean writeEnabled;

  /** 节点权限档位；写入兼容字段由该值派生。 */
  @Column(name = "permission_profile", nullable = false)
  String permissionProfile = "read_only";

  /** 可选的步骤模型覆盖值；为空时继承 SOP 默认模型。 */
  @Column(name = "model_override")
  String modelOverride;

  /** 单步骤执行超时时间，单位为秒。 */
  @Column(name = "timeout_sec", nullable = false)
  int timeoutSec = 1800;

  /** 步骤声明的 Skill 标签集合，映射到独立关联表并以 {@code step_id} 连接。 */
  @ElementCollection
  @CollectionTable(name = "codex_sop_step_skills", joinColumns = @JoinColumn(name = "step_id"))
  @Column(name = "tag")
  Set<String> skills = new LinkedHashSet<>();

  /** 步骤声明的 MCP 标签集合，映射到独立关联表并以 {@code step_id} 连接。 */
  @ElementCollection
  @CollectionTable(name = "codex_sop_step_mcps", joinColumns = @JoinColumn(name = "step_id"))
  @Column(name = "tag")
  Set<String> mcps = new LinkedHashSet<>();
}
