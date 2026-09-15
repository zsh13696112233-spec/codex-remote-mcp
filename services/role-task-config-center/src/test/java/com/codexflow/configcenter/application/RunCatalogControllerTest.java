package com.codexflow.configcenter.application;

import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.codexflow.configcenter.web.ApiExceptionHandler;
import com.codexflow.configcenter.web.RunCatalogController;
import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import tools.jackson.databind.ObjectMapper;

class RunCatalogControllerTest {
  @Test
  void queryParametersReachServiceAndInvalidFiltersReturn400() throws Exception {
    var service = mock(RunCatalogService.class);
    var mapper = new ObjectMapper();
    var mvc =
        MockMvcBuilders.standaloneSetup(new RunCatalogController(service))
            .setControllerAdvice(new ApiExceptionHandler(mapper))
            .build();
    when(service.list("测试", "schedule", "2026-09-14", "2026-09-14", 1, 20, ""))
        .thenReturn(mapper.createObjectNode().put("total", 0));
    mvc.perform(
            get("/api/task-runs")
                .param("name", "测试")
                .param("type", "schedule")
                .param("startDate", "2026-09-14")
                .param("endDate", "2026-09-14")
                .param("page", "1"))
        .andExpect(status().isOk());
    verify(service).list("测试", "schedule", "2026-09-14", "2026-09-14", 1, 20, "");
    when(service.list("", "web", "", "", 0, 20, ""))
        .thenThrow(new IllegalArgumentException("任务种类无效。"));
    mvc.perform(get("/api/task-runs").param("type", "web")).andExpect(status().isBadRequest());
  }
}
