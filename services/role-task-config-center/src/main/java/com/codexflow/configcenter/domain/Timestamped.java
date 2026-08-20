package com.codexflow.configcenter.domain;

import jakarta.persistence.Column;
import jakarta.persistence.MappedSuperclass;
import jakarta.persistence.PrePersist;
import jakarta.persistence.PreUpdate;
import java.time.Instant;

/** 为需要审计时间的 JPA 实体提供统一的创建时间和更新时间映射。 */
@MappedSuperclass
abstract class Timestamped {

  /** 映射数据库的 {@code created_at} 字段，保存记录首次持久化的 UTC 时间。 */
  @Column(name = "created_at", nullable = false)
  Instant createdAt;

  /** 映射数据库的 {@code updated_at} 字段，保存记录最近一次更新的 UTC 时间。 */
  @Column(name = "updated_at", nullable = false)
  Instant updatedAt;

  /** 实体首次写入数据库前，同时初始化创建时间和更新时间。 */
  @PrePersist
  void createTimestamps() {
    Instant now = Instant.now();
    createdAt = now;
    updatedAt = now;
  }

  /** 实体执行更新 SQL 前刷新更新时间。 */
  @PreUpdate
  void updateTimestamp() {
    updatedAt = Instant.now();
  }
}
