package com.codexflow.configcenter.integration.dingtalk;

import org.springframework.data.jpa.repository.JpaRepository;

interface DingTalkActionContextRepository
    extends JpaRepository<DingTalkActionContextEntity, String> {}
