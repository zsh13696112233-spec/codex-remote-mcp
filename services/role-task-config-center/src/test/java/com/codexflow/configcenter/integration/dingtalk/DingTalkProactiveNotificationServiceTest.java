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
      service.submitted("run", "已启动。");
    }
    verify(store).enqueueScheduledStart("run", "工作流编号：run\n任务：已启动。");
    verify(store).enqueueText("proactive-start:run", "run", "group", null, "工作流编号：run\n任务：已启动。");
  }
}
