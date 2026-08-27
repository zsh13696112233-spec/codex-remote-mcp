package com.codexflow.configcenter.integration.dingtalk;

import org.springframework.data.jpa.repository.JpaRepository;

interface DingTalkSettingsRepository extends JpaRepository<DingTalkSettingsEntity, Byte> {}
