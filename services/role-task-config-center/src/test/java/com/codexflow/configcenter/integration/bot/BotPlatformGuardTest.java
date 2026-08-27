package com.codexflow.configcenter.integration.bot;

import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.integration.dingtalk.DingTalkProperties;
import com.codexflow.configcenter.integration.feishu.FeishuProperties;
import org.junit.jupiter.api.Test;

/** 验证飞书和钉钉不能同时启用。 */
class BotPlatformGuardTest {

  @Test
  void rejectsEnablingDingTalkWhileFeishuIsEnabled() {
    FeishuProperties feishu = new FeishuProperties();
    feishu.setEnabled(true);
    BotPlatformGuard guard = new BotPlatformGuard(feishu, new DingTalkProperties());

    assertThatThrownBy(() -> guard.assertCanEnable("dingtalk", true))
        .isInstanceOf(ConflictFailure.class)
        .hasMessageContaining("先停用飞书");
  }

  @Test
  void rejectsEnablingFeishuWhileDingTalkIsEnabled() {
    DingTalkProperties dingtalk = new DingTalkProperties();
    dingtalk.setEnabled(true);
    BotPlatformGuard guard = new BotPlatformGuard(new FeishuProperties(), dingtalk);

    assertThatThrownBy(() -> guard.assertCanEnable("feishu", true))
        .isInstanceOf(ConflictFailure.class)
        .hasMessageContaining("先停用钉钉");
  }
}
