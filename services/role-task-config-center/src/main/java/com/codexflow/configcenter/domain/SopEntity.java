package com.codexflow.configcenter.domain;

import jakarta.persistence.CascadeType;
import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.OneToMany;
import jakarta.persistence.OrderBy;
import jakarta.persistence.Table;
import java.util.ArrayList;
import java.util.List;

/** SOP 聚合根实体，对应数据库表 {@code codex_sop_sops}。 */
@Entity
@Table(name = "codex_sop_sops")
class SopEntity extends Timestamped {
  @jakarta.persistence.Column(name = "group_id", length = 36)
  String groupId;

  /** SOP 主键，使用应用生成的 UUID。 */
  @Id String id;

  /** SOP 名称，数据库中不允许为空。 */
  @Column(nullable = false)
  String name;

  /** SOP 的可选说明，最长 2000 字符。 */
  @Column(length = 2000)
  String description;

  /** 主监督执行机标识；保存配置时不要求执行机在线。 */
  @Column(name = "supervisor_agent_id", nullable = false)
  String supervisorAgentId = "local";

  /** 步骤失败策略；当前业务规则固定为 {@code stop}。 */
  @Column(name = "failure_policy", nullable = false)
  String failurePolicy = "stop";

  /** 整个 SOP 的主监督超时时间，单位为秒。 */
  @Column(name = "supervisor_timeout_sec", nullable = false)
  int supervisorTimeoutSec = 7200;

  /** 成功步骤与下一步骤之间的流转方式。 */
  @Column(name = "advance_mode", nullable = false)
  String advanceMode = "automatic";

  @Column(name = "confirmation_minutes", nullable = false)
  int confirmationMinutes = 2;

  @Column(name = "blocked_minutes", nullable = false)
  int blockedMinutes = 30;

  /** 遗留非空列的持久化映射，新建固定为文字交接，不参与执行逻辑。 */
  @Column(name = "handoff_mode", nullable = false)
  String handoffMode = "legacy_text";

  /** 是否向钉钉展示思考摘要和工具调用；新建 SOP 默认关闭。 */
  @Column(name = "dingtalk_show_execution_details", nullable = false)
  boolean dingtalkShowExecutionDetails = false;

  /** 步骤未单独指定模型时使用的默认模型。 */
  @Column(name = "default_step_model", nullable = false)
  String defaultStepModel = "gpt-6.1-sol";

  @Column(name = "default_step_reasoning_effort", length = 32)
  String defaultStepReasoningEffort;

  /** SOP 是否可用于创建新的任务运行。 */
  @Column(nullable = false)
  boolean enabled = true;

  /** 软删除标记；删除后的 SOP 不再出现在配置列表中，但历史任务仍可保留外键引用。 */
  @Column(nullable = false)
  boolean deleted = false;

  /** SOP 包含的有序步骤。保存 SOP 时级联保存步骤，移除集合元素时删除对应步骤；读取时按 {@code positionNo} 升序排列。 */
  @OneToMany(mappedBy = "sop", cascade = CascadeType.ALL, orphanRemoval = true)
  @OrderBy("positionNo ASC")
  List<SopStepEntity> steps = new ArrayList<>();
}
