package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.dto.RoleSaveRequest;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import tools.jackson.databind.node.JsonNodeFactory;

@SpringBootTest(
    properties =
        "spring.datasource.url=jdbc:h2:mem:role-group-names;MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1")
class RoleGroupNameIntegrationTest {
  @Autowired ConfigService configs;
  @MockitoBean GatewayClient gateway;

  @Test
  void namesAreScopedAndConflictingBatchRollsBack() {
    String a = "20000000-0000-0000-0000-000000000001";
    String b = "20000000-0000-0000-0000-000000000002";
    String c = "20000000-0000-0000-0000-000000000003";
    var directory = JsonNodeFactory.instance.objectNode();
    var groups = directory.putArray("groups");
    for (String id : List.of(a, b, c)) groups.addObject().put("id", id).put("name", id);
    when(gateway.get("/agent-groups")).thenReturn(directory);
    var first = configs.createRole(new RoleSaveRequest("Developer", "职责", false, null, a));
    var second = configs.createRole(new RoleSaveRequest("developer", "职责", true, null, b));
    String firstId = first.path("id").asText(), secondId = second.path("id").asText();
    assertThatThrownBy(
            () -> configs.createRole(new RoleSaveRequest(" developer ", "职责", true, null, a)))
        .isInstanceOf(ConflictFailure.class)
        .hasMessage("当前分组内角色名称已存在。");
    assertThatThrownBy(
            () ->
                configs.updateRole(
                    secondId,
                    new RoleSaveRequest(
                        "Developer", "职责", true, second.path("version").asLong(), a)))
        .isInstanceOf(ConflictFailure.class);
    assertThatThrownBy(() -> configs.assignGroups("roles", List.of(secondId), a))
        .isInstanceOf(ConflictFailure.class);
    assertThatThrownBy(() -> configs.assignGroups("roles", List.of(firstId, secondId), c))
        .isInstanceOf(ConflictFailure.class)
        .hasMessage("当前分组内角色名称已存在。");
    assertThat(configs.listRoles("developer", a)).hasSize(1);
    assertThat(configs.listRoles("developer", b)).hasSize(1);
    assertThat(configs.listRoles("developer", c)).isEmpty();
    configs.updateRole(
        secondId,
        new RoleSaveRequest("OtherDeveloper", "职责", true, second.path("version").asLong(), a));
    configs.assignGroups("roles", List.of(firstId), c);
    configs.deleteRole(firstId);
    configs.createRole(new RoleSaveRequest("DEVELOPER", "职责", true, null, c));
    assertThat(configs.listRoles("developer", c)).hasSize(1);
  }
}
