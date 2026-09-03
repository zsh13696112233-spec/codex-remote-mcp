package com.codexflow.configcenter.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;

/** 可供 SOP 选择的钉钉人员或群。 */
@Entity
@Table(name = "codex_sop_dingtalk_targets")
class DingTalkTargetEntity extends Timestamped {

  @Id String id;

  @Column(name = "client_id", nullable = false, length = 128)
  String clientId;

  @Column(name = "target_type", nullable = false, length = 16)
  String targetType;

  @Column(name = "external_id", nullable = false, length = 256)
  String externalId;

  @Column(name = "display_name", nullable = false, length = 160)
  String displayName;

  @Column(name = "department_display", length = 1000)
  String departmentDisplay;

  @Column(nullable = false, length = 16)
  String source;

  @Column(nullable = false)
  boolean available = true;

  @Column(nullable = false)
  boolean enabled;

  @Column(nullable = false)
  boolean deleted;

  @Column(name = "last_synced_at")
  Instant lastSyncedAt;
}
