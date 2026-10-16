package com.codexflow.configcenter.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.client.GatewayFailure;
import com.codexflow.configcenter.domain.TaskScheduleStore;
import com.codexflow.configcenter.dto.ScheduleCheckRequest;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.node.JsonNodeFactory;

class ScheduleCheckServiceTest {
  @Test
  void onlyPassedCurrentCheckLaunchesAndFailuresAreRecordedWithoutRetry() {
    for (String status : new String[] {"passed", "skipped", "failed", "invalid", "unavailable"}) {
      var store = mock(TaskScheduleStore.class);
      var gateway = mock(GatewayClient.class);
      var dispatch =
          new TaskScheduleStore.Dispatch(
              "task",
              "token",
              "group",
              new ScheduleCheckRequest(true, "machine", "E:/check.bat", "-user PlanAgent", 60));
      when(store.startCheck(dispatch)).thenReturn(true);
      when(store.finishCheck(eq(dispatch), anyString(), anyString())).thenReturn(true);
      if (status.equals("unavailable"))
        when(gateway.checkSchedule(any())).thenThrow(new GatewayFailure(502, "private"));
      else
        when(gateway.checkSchedule(any()))
            .thenReturn(
                JsonNodeFactory.instance.objectNode().put("status", status).put("reason", "检查结果"));
      assertThat(new ScheduleCheckService(store, gateway).passes(dispatch))
          .isEqualTo(status.equals("passed"));
      verify(gateway, times(1)).checkSchedule(any());
      verify(store)
          .finishCheck(
              eq(dispatch),
              eq(java.util.Set.of("invalid", "unavailable").contains(status) ? "failed" : status),
              argThat(reason -> !reason.contains("private")));
    }
  }

  @Test
  void disabledAndInvalidatedChecksDoNotExecuteRemoteCommand() {
    var store = mock(TaskScheduleStore.class);
    var gateway = mock(GatewayClient.class);
    var service = new ScheduleCheckService(store, gateway);
    assertThat(
            service.passes(
                new TaskScheduleStore.Dispatch(
                    "task", "t", "g", new ScheduleCheckRequest(false, null, null, null, 60))))
        .isTrue();
    assertThat(
            service.passes(
                new TaskScheduleStore.Dispatch(
                    "task", "t", "g", new ScheduleCheckRequest(true, "m", "E:/c.cmd", "", 60))))
        .isFalse();
    verifyNoInteractions(gateway);
  }
}
