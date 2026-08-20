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

  /** 角色主键，使用应用生成的 UUID，对应表的 {@code id} 字段。 */
  @Id String id;

  /** 角色名称；数据库非空且唯一，用于页面展示和名称检索。 */
  @Column(nullable = false, unique = true)
  String name;

  /** 角色职责说明，对应最长 2000 字符的 {@code duty} 字段。 */
  @Column(nullable = false, length = 2000)
  String duty;

  /** 是否允许新建 SOP 步骤继续引用该角色。 */
  @Column(nullable = false)
  boolean enabled = true;

  /** JPA 乐观锁版本号，更新角色时用于检测并发覆盖。 */
  @Version long version;
}
