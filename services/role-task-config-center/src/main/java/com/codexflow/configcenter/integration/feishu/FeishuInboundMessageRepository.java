package com.codexflow.configcenter.integration.feishu;

import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

interface FeishuInboundMessageRepository extends JpaRepository<FeishuInboundMessageEntity, String> {

  Optional<FeishuInboundMessageEntity> findByWorkflowIdAndWorkflowMessageId(
      String workflowId, String workflowMessageId);
}
