package com.codexflow.configcenter.integration.dingtalk;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.DingTalkTaskBindingDirectory;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;

class DingTalkProactiveNotificationServiceTest {
  @Test
  void onlyScheduledStartUsesMonitorButton() {
    var json = new ObjectMapper();
    var store = mock(DingTalkStore.class);
    var gateway = mock(GatewayClient.class);
    var service =
        new DingTalkProactiveNotificationService(
            mock(DingTalkSettingsStore.class),
            mock(DingTalkTaskBindingDirectory.class),
            store,
            gateway,
            mock(DingTalkProgressCard.class),
            json);
    when(gateway.get("/workflows/run")).thenReturn(json.createObjectNode().put("name", "任务"));
    for (String source : new String[] {"schedule", "web"}) {
      when(store.binding("run"))
          .thenReturn(
              Optional.of(
                  new DingTalkModels.Binding(
                      "run", "group", "GROUP", "group", "群", null, source, "active", 0, null,
                      false)));
      service.submitted("run", "已启动。", json.createObjectNode());
    }
    verify(store).enqueueScheduledStart("run", "工作流编号：run\n任务：已启动。");
    verify(store).enqueueText("proactive-start:run", "run", "group", null, "工作流编号：run\n任务：已启动。");
  }

  @Test
  void scheduledAndWebNotificationsIncludeFrozenJiraTitle() {
    var json = new ObjectMapper();
    var store = mock(DingTalkStore.class);
    var gateway = mock(GatewayClient.class);
    var service =
        new DingTalkProactiveNotificationService(
            mock(DingTalkSettingsStore.class),
            mock(DingTalkTaskBindingDirectory.class),
            store,
            gateway,
            mock(DingTalkProgressCard.class),
            json);
    when(gateway.get("/workflows/run")).thenReturn(json.createObjectNode().put("name", "调度测试"));
    var issue =
        json.createObjectNode()
            .put("issueKey", "TEST-1")
            .put("title", "修复 *筛选* 异常")
            .put("server", "http://jira.internal")
            .put("accountId", "private-account");
    for (String source : new String[] {"schedule", "web"}) {
      when(store.binding("run"))
          .thenReturn(
              Optional.of(
                  new DingTalkModels.Binding(
                      "run", "group", "GROUP", "group", "群", null, source, "active", 0, null,
                      false)));
      service.submitted("run", "已领取 Jira 工单并启动任务。", issue);
    }
    String text = "工作流编号：run\n调度测试：已领取 Jira 工单并启动任务。\nJira：TEST-1\n工单标题：修复 *筛选* 异常";
    verify(store).enqueueScheduledStart("run", text.replace("*", "\\*"));
    verify(store).enqueueText("proactive-start:run", "run", "group", null, text);
  }
}
