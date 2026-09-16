package com.codexflow.configcenter.application;

import static org.mockito.AdditionalMatchers.aryEq;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.web.ApiExceptionHandler;
import com.codexflow.configcenter.web.SkillController;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.http.ResponseEntity;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import tools.jackson.databind.ObjectMapper;

class SkillControllerTest {
  @Test
  void groupFiltersAssignmentAndRequiredUploadGroup() throws Exception {
    var gateway = mock(GatewayClient.class);
    var mapper = new ObjectMapper();
    var mvc =
        MockMvcBuilders.standaloneSetup(new SkillController(new SkillService(gateway), mapper))
            .setControllerAdvice(new ApiExceptionHandler(mapper))
            .build();
    String group = UUID.randomUUID().toString();
    for (String path :
        new String[] {"/skills", "/skills/machines", "/skills/inventory", "/skill-deployments"}) {
      when(gateway.skillExchange("GET", path + "?groupId=" + group, null, "application/json"))
          .thenReturn(ResponseEntity.ok(mapper.createObjectNode()));
      mvc.perform(get("/api" + path).param("groupId", group)).andExpect(status().isOk());
      verify(gateway).skillExchange("GET", path + "?groupId=" + group, null, "application/json");
      mvc.perform(get("/api" + path).param("groupId", "bad&extra=value"))
          .andExpect(status().isBadRequest());
    }
    mvc.perform(post("/api/skills").contentType("application/zip").content(new byte[] {80, 75}))
        .andExpect(status().isBadRequest());
    when(gateway.skillExchange(
            eq("POST"), eq("/skills/groups/assign"), any(byte[].class), eq("application/json")))
        .thenReturn(
            ResponseEntity.status(409).body(mapper.createObjectNode().put("error", "分组不存在")));
    mvc.perform(
            post("/api/skills/groups/assign")
                .content("{\"groupId\":\"" + group + "\",\"packageIds\":[\"a\"]}"))
        .andExpect(status().isConflict())
        .andExpect(jsonPath("$.error").value("分组不存在"));
    mvc.perform(post("/api/skills/groups/assign").content(new byte[32769]))
        .andExpect(status().isPayloadTooLarge());
  }

  @Test
  void directoryCheckProxiesRegisteredMachineAndPreservesFailure() throws Exception {
    var gateway = mock(GatewayClient.class);
    var mapper = new ObjectMapper();
    var mvc =
        MockMvcBuilders.standaloneSetup(new SkillController(new SkillService(gateway), mapper))
            .setControllerAdvice(new ApiExceptionHandler(mapper))
            .build();
    String id = "machine-" + UUID.randomUUID().toString().replace("-", "");
    when(gateway.skillExchange(
            eq("POST"),
            eq("/skills/machines/" + id + "/check"),
            aryEq("{}".getBytes(java.nio.charset.StandardCharsets.UTF_8)),
            eq("application/json")))
        .thenReturn(
            ResponseEntity.status(409).body(mapper.createObjectNode().put("error", "目录未配置")));
    mvc.perform(post("/api/skills/machines/" + id + "/check")).andExpect(status().isConflict());
    mvc.perform(post("/api/skills/machines/invalid/check")).andExpect(status().isBadRequest());
    verify(gateway, times(1)).skillExchange(anyString(), anyString(), any(), anyString());
  }

  @Test
  void uploadPreservesBytesAndConflictStatus() throws Exception {
    var gateway = mock(GatewayClient.class);
    var mapper = new ObjectMapper();
    var mvc =
        MockMvcBuilders.standaloneSetup(new SkillController(new SkillService(gateway), mapper))
            .setControllerAdvice(new ApiExceptionHandler(mapper))
            .build();
    String group = UUID.randomUUID().toString();
    byte[] content = {80, 75, 0, -1};
    when(gateway.skillExchange(
            eq("POST"), eq("/skills?groupId=" + group), aryEq(content), eq("application/zip")))
        .thenReturn(
            ResponseEntity.status(409).body(mapper.createObjectNode().put("error", "同名内容冲突。")));
    mvc.perform(
            post("/api/skills")
                .param("groupId", group)
                .contentType("application/zip")
                .content(content))
        .andExpect(status().isConflict())
        .andExpect(jsonPath("$.error").value("同名内容冲突。"));
    verify(gateway)
        .skillExchange(
            eq("POST"), eq("/skills?groupId=" + group), aryEq(content), eq("application/zip"));
  }

  @Test
  void rejectsOversizedUploadBeforeProxyAndInvalidBodies() throws Exception {
    var gateway = mock(GatewayClient.class);
    var mapper = new ObjectMapper();
    var mvc =
        MockMvcBuilders.standaloneSetup(new SkillController(new SkillService(gateway), mapper))
            .setControllerAdvice(new ApiExceptionHandler(mapper))
            .build();
    mvc.perform(
            post("/api/skills")
                .contentType("application/zip")
                .content(new byte[20 * 1024 * 1024 + 1]))
        .andExpect(status().isPayloadTooLarge());
    mvc.perform(post("/api/skills").contentType("text/plain").content("bad"))
        .andExpect(status().isUnsupportedMediaType());
    mvc.perform(post("/api/skill-deployments").content("[1]")).andExpect(status().isBadRequest());
    mvc.perform(post("/api/skill-deployments").content("bad JSON"))
        .andExpect(status().isBadRequest());
    mvc.perform(post("/api/skill-deployments").content(new byte[32769]))
        .andExpect(status().isPayloadTooLarge());
    mvc.perform(post("/api/skill-deployment-tasks/" + UUID.randomUUID() + "/remove").content("{}"))
        .andExpect(status().isBadRequest());
    verifyNoInteractions(gateway);
  }

  @Test
  void asyncCreateAndReadUseCentralGatewayOnly() throws Exception {
    var gateway = mock(GatewayClient.class);
    var mapper = new ObjectMapper();
    var mvc =
        MockMvcBuilders.standaloneSetup(new SkillController(new SkillService(gateway), mapper))
            .build();
    String id = UUID.randomUUID().toString();
    when(gateway.skillExchange(
            eq("POST"), eq("/skill-deployments"), any(byte[].class), eq("application/json")))
        .thenReturn(ResponseEntity.accepted().body(mapper.createObjectNode().put("id", id)));
    mvc.perform(post("/api/skill-deployments").content("{\"packageId\":\"sample\"}"))
        .andExpect(status().isAccepted())
        .andExpect(jsonPath("$.id").value(id));
    when(gateway.skillExchange("GET", "/skill-deployments/" + id, null, "application/json"))
        .thenReturn(ResponseEntity.ok(mapper.createObjectNode().put("id", id)));
    mvc.perform(get("/api/skill-deployments/" + id)).andExpect(status().isOk());
    verify(gateway).skillExchange("GET", "/skill-deployments/" + id, null, "application/json");
    String packageId = "a".repeat(64);
    when(gateway.skillExchange("GET", "/skills/" + packageId, null, "application/json"))
        .thenReturn(ResponseEntity.ok(mapper.createObjectNode().put("id", packageId)));
    mvc.perform(get("/api/skills/" + packageId)).andExpect(status().isOk());
    verify(gateway).skillExchange("GET", "/skills/" + packageId, null, "application/json");
    when(gateway.skillExchange("GET", "/skills/inventory", null, "application/json"))
        .thenReturn(
            ResponseEntity.ok(
                mapper.createObjectNode().putArray("items").addObject().put("name", "demo")));
    mvc.perform(get("/api/skills/inventory")).andExpect(status().isOk());
    verify(gateway).skillExchange("GET", "/skills/inventory", null, "application/json");
  }
}
