package com.codexflow.configcenter.application;

import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.domain.TaskScheduleStore;
import com.codexflow.configcenter.web.ApiExceptionHandler;
import com.codexflow.configcenter.web.TaskScheduleController;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import tools.jackson.databind.ObjectMapper;

class TaskScheduleControllerTest {
  @Test
  void validatesWritesAndMapsConflicts() throws Exception {
    var store = mock(TaskScheduleStore.class);
    var mvc =
        MockMvcBuilders.standaloneSetup(new TaskScheduleController(store))
            .setControllerAdvice(new ApiExceptionHandler(new ObjectMapper()))
            .build();
    mvc.perform(get("/api/task-schedules").param("q", "日报")).andExpect(status().isOk());
    verify(store).list("日报", "");
    String valid =
        """
        {"groupId":"00000000-0000-0000-0000-000000000001","name":"日报","sopId":"sop","taskDefinitionId":"task","mode":"interval","intervalMinutes":40,"enabled":true}
        """;
    mvc.perform(post("/api/task-schedules").contentType(MediaType.APPLICATION_JSON).content(valid))
        .andExpect(status().isCreated());
    mvc.perform(
            post("/api/task-schedules")
                .contentType(MediaType.APPLICATION_JSON)
                .content(valid.replace(":40", ":4")))
        .andExpect(status().isBadRequest());
    when(store.save(eq("existing"), any())).thenThrow(new ConflictFailure("任务已有配置"));
    mvc.perform(
            put("/api/task-schedules/existing")
                .contentType(MediaType.APPLICATION_JSON)
                .content(valid))
        .andExpect(status().isConflict());
    mvc.perform(delete("/api/task-schedules/existing")).andExpect(status().isNoContent());
    verify(store).delete("existing");
  }
}
