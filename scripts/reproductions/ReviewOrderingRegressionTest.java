package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
class ReviewOrderingRegressionTest {
  @Autowired DingTalkStore store;

  @Test
  void laterProgressMustWaitForEarlierFailedProgress() {
    store.enqueueTargetText("review-first", null, "review-group", "GROUP", "review-group", null, "步骤开始");
    var first = store.claimDue().get(0);
    store.markOutboxFailed(first.id(), new IllegalStateException("temporary transport failure"));
    store.enqueueTargetText("review-second", null, "review-group", "GROUP", "review-group", null, "步骤完成");
    assertThat(store.claimDue()).as("前一条仍在退避重试，后一条不能先发送").isEmpty();
  }
}
