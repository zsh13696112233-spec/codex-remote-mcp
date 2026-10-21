package com.codexflow.configcenter.integration.dingtalk;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

/** 平台生成的监控入口，在业务正文过滤后添加。 */
final class DingTalkMonitorLink {
  private DingTalkMonitorLink() {}

  static String markdown(String baseUrl, String workflowId) {
    return "[查看监控]("
        + baseUrl.replaceAll("/+$", "")
        + "/?workflowId="
        + URLEncoder.encode(workflowId, StandardCharsets.UTF_8)
        + ")";
  }
}
