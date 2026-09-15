package com.codexflow.configcenter;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.client.GatewayClient;
import org.junit.jupiter.api.BeforeEach;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import tools.jackson.databind.node.JsonNodeFactory;

/** 既有业务回归测试的显式分组环境，不改变生产兼容规则。 */
public abstract class GroupedFixtureSupport {
  public static final String GROUP = "00000000-0000-0000-0000-000000000001";
  @MockitoBean protected GatewayClient groupGateway;
  @Autowired private JdbcTemplate groupJdbc;

  @BeforeEach
  void prepareGroupFixture() {
    var directory = JsonNodeFactory.instance.objectNode();
    directory.putArray("groups").addObject().put("id", GROUP).put("name", "测试组");
    when(groupGateway.get("/agent-groups")).thenReturn(directory);
    when(groupGateway.post(eq("/agents/validate"), any()))
        .thenReturn(JsonNodeFactory.instance.objectNode());
    groupJdbc.update("UPDATE codex_sop_roles SET group_id=? WHERE group_id IS NULL", GROUP);
  }

  public static <T extends Record> T grouped(T request) {
    try {
      var fields = request.getClass().getRecordComponents();
      Object[] args = new Object[fields.length];
      Class<?>[] types = new Class<?>[fields.length];
      for (int i = 0; i < fields.length; i++) {
        types[i] = fields[i].getType();
        args[i] =
            fields[i].getName().equals("groupId") ? GROUP : fields[i].getAccessor().invoke(request);
      }
      @SuppressWarnings("unchecked")
      T result = (T) request.getClass().getDeclaredConstructor(types).newInstance(args);
      return result;
    } catch (ReflectiveOperationException error) {
      throw new AssertionError(error);
    }
  }
}
