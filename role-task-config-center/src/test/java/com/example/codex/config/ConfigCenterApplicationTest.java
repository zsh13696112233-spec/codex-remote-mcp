package com.example.codex.config;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;
import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest
class ConfigCenterApplicationTest {
    @Autowired JdbcTemplate jdbc;
    @Autowired ConfigService service;
    @Autowired ObjectMapper mapper;
    @Test void contextLoadsWithFlywaySchemaAndSeedData() {
        assertThat(jdbc.queryForObject("select count(*) from codex_sop_roles", Integer.class)).isEqualTo(3);
        assertThat(jdbc.queryForObject("select count(*) from codex_sop_task_runs", Integer.class)).isZero();
    }

    @Test void loadedRelationshipsAreAvailableWhenBuildingApiSnapshots() {
        String roleId=jdbc.queryForObject("select id from codex_sop_roles order by created_at limit 1",String.class);
        ObjectNode sopBody=mapper.createObjectNode();
        sopBody.put("name","关系加载回归测试");
        var rawSteps=sopBody.putArray("steps");
        var rawStep=rawSteps.addObject();
        rawStep.put("displayName","执行步骤");
        rawStep.put("roleId",roleId);
        rawStep.put("instruction","完成测试步骤");
        rawStep.put("agentId","local");
        ObjectNode createdSop=service.createSop(sopBody);

        ObjectNode taskBody=mapper.createObjectNode();
        taskBody.put("name","关系加载任务");
        taskBody.put("objective","验证任务关联的 SOP 可在新事务中加载");
        taskBody.put("sopId",createdSop.path("id").asText());
        ObjectNode createdTask=service.createTask(taskBody);

        ObjectNode loadedSop=service.getSop(createdSop.path("id").asText());
        ObjectNode loadedTask=service.getTask(createdTask.path("id").asText());
        assertThat(loadedSop.path("steps").get(0).path("roleId").asText()).isEqualTo(roleId);
        assertThat(loadedSop.path("steps").get(0).path("roleName").asText()).isNotBlank();
        assertThat(loadedTask.path("sopId").asText()).isEqualTo(createdSop.path("id").asText());
        assertThat(loadedTask.path("sopName").asText()).isEqualTo("关系加载回归测试");
    }
}
