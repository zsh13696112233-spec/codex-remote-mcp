package com.codexflow.configcenter.integration.bot;

import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.integration.dingtalk.DingTalkProperties;
import com.codexflow.configcenter.integration.feishu.FeishuProperties;
import org.springframework.stereotype.Component;

/** 确保同一配置中心只启用一个外部机器人平台。 */
@Component
public class BotPlatformGuard {

  private final FeishuProperties feishu;
  private final DingTalkProperties dingtalk;

  public BotPlatformGuard(FeishuProperties feishu, DingTalkProperties dingtalk) {
    this.feishu = feishu;
    this.dingtalk = dingtalk;
  }

  public void assertCanEnable(String platform, boolean enabled) {
    if (!enabled) return;
    if ("feishu".equals(platform) && dingtalk.isEnabled()) {
      throw new ConflictFailure("钉钉机器人已启用，请先停用钉钉机器人后再启用飞书。");
    }
    if ("dingtalk".equals(platform) && feishu.isEnabled()) {
      throw new ConflictFailure("飞书机器人已启用，请先停用飞书机器人后再启用钉钉。");
    }
  }
}
