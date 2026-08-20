CREATE TABLE codex_sop_roles (
  id VARCHAR(36) PRIMARY KEY COMMENT '角色ID（UUID）',
  name VARCHAR(100) NOT NULL UNIQUE COMMENT '角色名称',
  duty VARCHAR(2000) NOT NULL COMMENT '角色职责说明',
  enabled BOOLEAN NOT NULL DEFAULT TRUE COMMENT '是否启用',
  version BIGINT NOT NULL DEFAULT 0 COMMENT '乐观锁版本号',
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间'
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='角色定义表';
CREATE TABLE codex_sop_sops (
  id VARCHAR(36) PRIMARY KEY COMMENT 'SOP ID（UUID）',
  name VARCHAR(160) NOT NULL COMMENT 'SOP名称',
  description VARCHAR(2000) COMMENT 'SOP说明',
  supervisor_agent_id VARCHAR(128) NOT NULL COMMENT '主监督执行机ID',
  failure_policy VARCHAR(20) NOT NULL DEFAULT 'stop' COMMENT '步骤失败策略，当前固定为stop',
  supervisor_timeout_sec INT NOT NULL DEFAULT 7200 COMMENT '主监督超时时间（秒）',
  default_step_model VARCHAR(64) NOT NULL COMMENT '步骤默认模型ID',
  enabled BOOLEAN NOT NULL DEFAULT TRUE COMMENT '是否启用',
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间'
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='标准作业流程定义表';
CREATE TABLE codex_sop_steps (
  id VARCHAR(36) PRIMARY KEY COMMENT '步骤ID（UUID）',
  sop_id VARCHAR(36) NOT NULL COMMENT '所属SOP ID',
  position_no INT NOT NULL COMMENT '步骤顺序号，从小到大串行执行',
  display_name VARCHAR(160) NOT NULL COMMENT '步骤显示名称',
  role_id VARCHAR(36) NOT NULL COMMENT '执行角色ID',
  instruction_text LONGTEXT NOT NULL COMMENT '步骤执行指令',
  expected_output LONGTEXT NOT NULL COMMENT '预期输出说明',
  executor_type VARCHAR(16) NOT NULL COMMENT '执行器类型',
  agent_id VARCHAR(128) NOT NULL COMMENT '执行机ID',
  working_directory VARCHAR(1000) COMMENT '工作目录，为空时使用执行机默认目录',
  write_enabled BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否允许写入文件',
  model_override VARCHAR(64) COMMENT '覆盖模型ID，为空时继承SOP默认模型',
  timeout_sec INT NOT NULL DEFAULT 1800 COMMENT '步骤超时时间（秒）',
  CONSTRAINT fk_step_sop FOREIGN KEY (sop_id) REFERENCES codex_sop_sops(id),
  CONSTRAINT fk_step_role FOREIGN KEY (role_id) REFERENCES codex_sop_roles(id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='SOP串行步骤表';
CREATE TABLE codex_sop_step_skills (
  step_id VARCHAR(36) NOT NULL COMMENT '步骤ID',
  tag VARCHAR(160) NOT NULL COMMENT 'Skill标签',
  PRIMARY KEY(step_id, tag),
  CONSTRAINT fk_skill_step FOREIGN KEY(step_id) REFERENCES codex_sop_steps(id) ON DELETE CASCADE
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='SOP步骤Skill标签表';
CREATE TABLE codex_sop_step_mcps (
  step_id VARCHAR(36) NOT NULL COMMENT '步骤ID',
  tag VARCHAR(160) NOT NULL COMMENT 'MCP标签',
  PRIMARY KEY(step_id, tag),
  CONSTRAINT fk_mcp_step FOREIGN KEY(step_id) REFERENCES codex_sop_steps(id) ON DELETE CASCADE
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='SOP步骤MCP标签表';
CREATE TABLE codex_sop_task_definitions (
  id VARCHAR(36) PRIMARY KEY COMMENT '任务定义ID（UUID）',
  name VARCHAR(160) NOT NULL COMMENT '任务名称',
  objective LONGTEXT NOT NULL COMMENT '任务目标',
  sop_id VARCHAR(36) NOT NULL COMMENT '关联的SOP ID',
  additional_notes LONGTEXT COMMENT '补充说明',
  enabled BOOLEAN NOT NULL DEFAULT TRUE COMMENT '是否启用',
  deleted BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否已软删除',
  created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间',
  CONSTRAINT fk_task_sop FOREIGN KEY(sop_id) REFERENCES codex_sop_sops(id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='任务定义表';
CREATE TABLE codex_sop_task_runs (
  workflow_id VARCHAR(128) PRIMARY KEY COMMENT '本次运行的工作流ID（UUID）',
  task_definition_id VARCHAR(36) NOT NULL COMMENT '任务定义ID',
  source_workflow_id VARCHAR(128) COMMENT '按原快照重试时的来源工作流ID',
  status VARCHAR(32) NOT NULL COMMENT '运行提交状态',
  snapshot_json LONGTEXT NOT NULL COMMENT '提交时的不可变业务快照JSON',
  submitted_json LONGTEXT NOT NULL COMMENT '发送给Python网关的完整JSON',
  gateway_response_json LONGTEXT COMMENT 'Python网关成功响应JSON',
  error_message LONGTEXT COMMENT '提交或运行失败详情',
  submitted_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '提交时间',
  updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最后更新时间',
  CONSTRAINT fk_run_task FOREIGN KEY(task_definition_id) REFERENCES codex_sop_task_definitions(id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT='任务运行及不可变快照表';
CREATE INDEX idx_steps_role ON codex_sop_steps(role_id);
CREATE INDEX idx_tasks_sop ON codex_sop_task_definitions(sop_id);
CREATE INDEX idx_runs_task ON codex_sop_task_runs(task_definition_id, submitted_at);

INSERT INTO codex_sop_roles(id,name,duty,enabled,version,created_at,updated_at) VALUES
('00000000-0000-0000-0000-000000000001','策略负责人','分析任务目标，拆分工作计划，协调各执行角色并完成最终结果验收。',TRUE,0,CURRENT_TIMESTAMP(6),CURRENT_TIMESTAMP(6)),
('00000000-0000-0000-0000-000000000002','开发工程师','负责功能实现、代码修改、依赖构建与本地测试，提交可验证的交付结果。',TRUE,0,CURRENT_TIMESTAMP(6),CURRENT_TIMESTAMP(6)),
('00000000-0000-0000-0000-000000000003','质量审查员','检查代码质量、安全风险和测试覆盖情况，并给出明确的改进建议。',TRUE,0,CURRENT_TIMESTAMP(6),CURRENT_TIMESTAMP(6));
