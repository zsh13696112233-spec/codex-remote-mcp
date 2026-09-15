package com.codexflow.configcenter.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.RunCatalogStore;
import com.codexflow.configcenter.domain.WorkflowRunStore;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

class RunCatalogServiceTest {
  private final ObjectMapper mapper = new ObjectMapper();
  private final RunCatalogStore catalog = mock(RunCatalogStore.class);
  private final WorkflowRunStore runs = mock(WorkflowRunStore.class);
  private final GatewayClient gateway = mock(GatewayClient.class);
  private final RunCatalogService service = new RunCatalogService(catalog, runs, gateway);

  private ObjectNode result() {
    ObjectNode result = mapper.createObjectNode().put("total", 2);
    var items = result.putArray("items");
    items.addObject().put("workflowId", "one").put("status", "completed");
    items.addObject().put("workflowId", "two").put("status", "submit_failed");
    when(catalog.list("", "", "", "", 0, 20, "")).thenReturn(result);
    return result;
  }

  @Test
  void completedRunsCanResumeAndOnlyRequestedStatusesArePersisted() {
    result();
    ObjectNode live = mapper.createObjectNode();
    live.putObject("statuses").put("one", "running").put("unrequested", "failed");
    when(gateway.post(eq("/workflow-statuses"), any())).thenReturn(live);
    ObjectNode response = service.list("", "", "", "", 0, 20, "");
    assertThat(response.path("items").get(0).path("status").asText()).isEqualTo("running");
    assertThat(response.path("items").get(1).path("status").asText()).isEqualTo("submit_failed");
    assertThat(response.path("statusFresh").asBoolean()).isTrue();
    verify(runs).recordSummaryStatuses(mapper.createObjectNode().put("one", "running"));
    verify(gateway, times(1)).post(eq("/workflow-statuses"), any());
  }

  @Test
  void offlineOrInvalidStatusRetainsLastKnownResult() {
    result();
    when(gateway.post(eq("/workflow-statuses"), any()))
        .thenThrow(new IllegalStateException("offline"));
    ObjectNode response = service.list("", "", "", "", 0, 20, "");
    assertThat(response.path("statusFresh").asBoolean()).isFalse();
    assertThat(response.path("items").get(0).path("status").asText()).isEqualTo("completed");
    verifyNoInteractions(runs);
    ObjectNode invalid = mapper.createObjectNode();
    invalid.putObject("statuses").put("one", "unknown");
    when(gateway.post(eq("/workflow-statuses"), any())).thenReturn(invalid);
    assertThat(service.list("", "", "", "", 0, 20, "").path("statusFresh").asBoolean()).isFalse();
  }
}
