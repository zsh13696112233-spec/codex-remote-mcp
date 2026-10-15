package com.codexflow.console;

import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoMoreInteractions;

import com.codexflow.console.client.GatewayClient;
import com.codexflow.console.web.WorkflowController;
import org.junit.jupiter.api.Test;

class MonitorDetailsTest {
  @Test
  void detailsUsesExistingReadProxyAndPreservesPagingValidation() {
    GatewayClient gateway = mock(GatewayClient.class);
    WorkflowController controller = new WorkflowController(gateway);
    controller.events("one", 0, 200, "monitor_details", null, true);
    controller.events("one", 42, 200, "monitor_details", null, false);
    controller.events("one", 0, 200, "monitor_details", 42L, false);
    verify(gateway).events("one", 0, 200, "monitor_details", null, true);
    verify(gateway).events("one", 42, 200, "monitor_details", null, false);
    verify(gateway).events("one", 0, 200, "monitor_details", 42L, false);
    assertThatThrownBy(() -> controller.events("one", 42, 200, "monitor_details", null, true))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(() -> controller.events("one", 0, 200, "unknown", null, false))
        .isInstanceOf(IllegalArgumentException.class);
    verifyNoMoreInteractions(gateway);
  }
}
