package com.codexflow.configcenter.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;

/** 角色定义实体，对应数据库表 {@code codex_sop_roles}。 */
@Entity
@Table(name = "codex_sop_roles")
class RoleEntity extends Timestamped {
  @jakarta.persistence.Column(name = "group_id", length = 36)
  String groupId;

  /** 角色主键，使用应用生成的 UUID，对应表的 {@code id} 字段。 */
  @Id String id;

  /** 角色名称；未删除角色的名称唯一，用于页面展示和名称检索。 */
  @Column(nullable = false)
  String name;

  /** 角色职责说明，使用长文本保存；请求边界限制为 50,000 字符。 */
  @Column(nullable = false, columnDefinition = "LONGTEXT")
  String duty;

  /** 是否允许新建 SOP 步骤继续引用该角色。 */
  @Column(nullable = false)
  boolean enabled = true;

  /** 软删除标记；删除后的角色不再出现在配置列表中，但历史 SOP 步骤仍可保留外键引用。 */
  @Column(nullable = false)
  boolean deleted = false;

  /** JPA 乐观锁版本号，更新角色时用于检测并发覆盖。 */
  @Version long version;
}
