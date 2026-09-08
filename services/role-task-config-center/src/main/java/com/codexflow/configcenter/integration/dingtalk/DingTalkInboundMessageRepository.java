package com.codexflow.configcenter.integration.dingtalk;

import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

interface DingTalkInboundMessageRepository
    extends JpaRepository<DingTalkInboundMessageEntity, String> {

  boolean existsByWorkflowIdAndStatus(String workflowId, String status);

  Optional<DingTalkInboundMessageEntity> findByWorkflowIdAndWorkflowMessageId(
      String workflowId, String workflowMessageId);
}
