package com.codexflow.configcenter.application;

import static org.mockito.AdditionalMatchers.aryEq;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.web.ApiExceptionHandler;
import com.codexflow.configcenter.web.McpController;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.http.ResponseEntity;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import tools.jackson.databind.ObjectMapper;

class McpControllerTest {
  @Test
  void groupFiltersAssignmentAndRequiredUploadGroup() throws Exception {
    var gateway = mock(GatewayClient.class);
    var mapper = new ObjectMapper();
    var mvc =
        MockMvcBuilders.standaloneSetup(new McpController(new McpService(gateway), mapper))
            .setControllerAdvice(new ApiExceptionHandler(mapper))
            .build();
    String group = UUID.randomUUID().toString();
    for (String path :
        new String[] {
          "/mcp-packages", "/mcp-packages/machines", "/mcp-packages/inventory", "/mcp-deployments"
        }) {
      when(gateway.skillExchange("GET", path + "?groupId=" + group, null, "application/json"))
          .thenReturn(ResponseEntity.ok(mapper.createObjectNode()));
      mvc.perform(get("/api" + path).param("groupId", group)).andExpect(status().isOk());
      verify(gateway).skillExchange("GET", path + "?groupId=" + group, null, "application/json");
      mvc.perform(get("/api" + path).param("groupId", "bad&extra=value"))
          .andExpect(status().isBadRequest());
    }
    mvc.perform(
            post("/api/mcp-packages").contentType("application/zip").content(new byte[] {80, 75}))
        .andExpect(status().isBadRequest());
    when(gateway.skillExchange(
            eq("POST"),
            eq("/mcp-packages/groups/assign"),
            any(byte[].class),
            eq("application/json")))
        .thenReturn(
            ResponseEntity.status(409).body(mapper.createObjectNode().put("error", "分组不存在")));
    mvc.perform(
            post("/api/mcp-packages/groups/assign")
                .content("{\"groupId\":\"" + group + "\",\"packageIds\":[\"a\"]}"))
        .andExpect(status().isConflict())
        .andExpect(jsonPath("$.error").value("分组不存在"));
    mvc.perform(post("/api/mcp-packages/groups/assign").content(new byte[32769]))
        .andExpect(status().isPayloadTooLarge());
  }

  @Test
  void uploadPreservesBytesAndConflictStatus() throws Exception {
    var gateway = mock(GatewayClient.class);
    var mapper = new ObjectMapper();
    var mvc =
        MockMvcBuilders.standaloneSetup(new McpController(new McpService(gateway), mapper))
            .setControllerAdvice(new ApiExceptionHandler(mapper))
            .build();
    String group = UUID.randomUUID().toString();
    byte[] content = {80, 75, 0, -1};
    when(gateway.skillExchange(
            eq("POST"),
            eq("/mcp-packages?groupId=" + group),
            aryEq(content),
            eq("application/zip")))
        .thenReturn(
            ResponseEntity.status(409).body(mapper.createObjectNode().put("error", "同名内容冲突。")));
    mvc.perform(
            post("/api/mcp-packages")
                .param("groupId", group)
                .contentType("application/zip")
                .content(content))
        .andExpect(status().isConflict())
        .andExpect(jsonPath("$.error").value("同名内容冲突。"));
    verify(gateway)
        .skillExchange(
            eq("POST"),
            eq("/mcp-packages?groupId=" + group),
            aryEq(content),
            eq("application/zip"));
  }

  @Test
  void rejectsOversizedUploadBeforeProxyAndInvalidBodies() throws Exception {
    var gateway = mock(GatewayClient.class);
    var mapper = new ObjectMapper();
    var mvc =
        MockMvcBuilders.standaloneSetup(new McpController(new McpService(gateway), mapper))
            .setControllerAdvice(new ApiExceptionHandler(mapper))
            .build();
    mvc.perform(
            post("/api/mcp-packages")
                .contentType("application/zip")
                .content(new byte[20 * 1024 * 1024 + 1]))
        .andExpect(status().isPayloadTooLarge());
    mvc.perform(post("/api/mcp-packages").contentType("text/plain").content("bad"))
        .andExpect(status().isUnsupportedMediaType());
    mvc.perform(post("/api/mcp-deployments").content("[1]")).andExpect(status().isBadRequest());
    mvc.perform(post("/api/mcp-deployments").content("bad JSON"))
        .andExpect(status().isBadRequest());
    mvc.perform(post("/api/mcp-deployments").content(new byte[32769]))
        .andExpect(status().isPayloadTooLarge());
    mvc.perform(post("/api/mcp-deployment-tasks/" + UUID.randomUUID() + "/remove").content("{}"))
        .andExpect(status().isBadRequest());
    verifyNoInteractions(gateway);
  }

  @Test
  void asyncCreateAndReadUseCentralGatewayOnly() throws Exception {
    var gateway = mock(GatewayClient.class);
    var mapper = new ObjectMapper();
    var mvc =
        MockMvcBuilders.standaloneSetup(new McpController(new McpService(gateway), mapper)).build();
    String id = UUID.randomUUID().toString();
    when(gateway.skillExchange(
            eq("POST"), eq("/mcp-deployments"), any(byte[].class), eq("application/json")))
        .thenReturn(ResponseEntity.accepted().body(mapper.createObjectNode().put("id", id)));
    mvc.perform(post("/api/mcp-deployments").content("{\"packageId\":\"sample\"}"))
        .andExpect(status().isAccepted())
        .andExpect(jsonPath("$.id").value(id));
    when(gateway.skillExchange("GET", "/mcp-deployments/" + id, null, "application/json"))
        .thenReturn(ResponseEntity.ok(mapper.createObjectNode().put("id", id)));
    mvc.perform(get("/api/mcp-deployments/" + id)).andExpect(status().isOk());
    verify(gateway).skillExchange("GET", "/mcp-deployments/" + id, null, "application/json");
    String packageId = "a".repeat(64);
    when(gateway.skillExchange("GET", "/mcp-packages/" + packageId, null, "application/json"))
        .thenReturn(ResponseEntity.ok(mapper.createObjectNode().put("id", packageId)));
    mvc.perform(get("/api/mcp-packages/" + packageId)).andExpect(status().isOk());
    verify(gateway).skillExchange("GET", "/mcp-packages/" + packageId, null, "application/json");
    when(gateway.skillExchange("GET", "/mcp-packages/inventory", null, "application/json"))
        .thenReturn(
            ResponseEntity.ok(
                mapper.createObjectNode().putArray("items").addObject().put("name", "demo")));
    mvc.perform(get("/api/mcp-packages/inventory")).andExpect(status().isOk());
    verify(gateway).skillExchange("GET", "/mcp-packages/inventory", null, "application/json");
  }
}
