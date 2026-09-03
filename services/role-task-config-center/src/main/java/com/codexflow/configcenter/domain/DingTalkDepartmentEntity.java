package com.codexflow.configcenter.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;

/** 钉钉通讯录中的部门节点，仅用于组织树展示。 */
@Entity
@Table(name = "codex_sop_dingtalk_departments")
class DingTalkDepartmentEntity extends Timestamped {

  @Id String id;

  @Column(name = "client_id", nullable = false, length = 128)
  String clientId;

  @Column(name = "external_id", nullable = false, length = 64)
  String externalId;

  @Column(name = "parent_external_id", length = 64)
  String parentExternalId;

  @Column(name = "display_name", nullable = false, length = 160)
  String displayName;

  @Column(nullable = false)
  boolean available = true;

  @Column(name = "last_synced_at")
  Instant lastSyncedAt;
}
