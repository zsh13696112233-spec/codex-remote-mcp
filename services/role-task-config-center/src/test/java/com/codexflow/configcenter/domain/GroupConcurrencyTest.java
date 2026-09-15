package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.dto.RoleSaveRequest;
import java.util.List;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import tools.jackson.databind.node.JsonNodeFactory;

@SpringBootTest(
    properties =
        "spring.datasource.url=jdbc:h2:mem:group-concurrency;MODE=MySQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_DELAY=-1;LOCK_TIMEOUT=10000")
class GroupConcurrencyTest {
  @Autowired GroupService groups;
  @Autowired ConfigService configs;
  @MockitoBean GatewayClient gateway;

  @Test
  void batchIsAtomicAndDeleteWaitsForConcurrentAssignment() throws Exception {
    String a = "10000000-0000-0000-0000-000000000001", b = "10000000-0000-0000-0000-000000000002";
    var directory = JsonNodeFactory.instance.objectNode();
    directory.putArray("groups").addObject().put("id", a).put("name", "并发甲组");
    directory.withArray("groups").addObject().put("id", b).put("name", "并发乙组");
    when(gateway.get("/agent-groups")).thenReturn(directory);
    var role = configs.createRole(new RoleSaveRequest("批量角色", "职责", true, null, a));
    String id = role.path("id").asText();
    assertThatThrownBy(() -> configs.assignGroups(null, List.of(id), b))
        .isInstanceOf(IllegalArgumentException.class);
    assertThat(configs.listRoles("批量", a)).hasSize(1);
    assertThat(configs.listRoles("批量", b)).isEmpty();
    assertThat(configs.listRoles("批量", "unassigned")).isEmpty();
    assertThatThrownBy(() -> configs.assignGroups("roles", List.of(id, "zz-missing"), b))
        .isInstanceOf(NotFoundFailure.class);
    assertThat(configs.listRoles("批量角色").get(0).path("groupId").asText()).isEqualTo(a);

    var entered = new CountDownLatch(1);
    var release = new CountDownLatch(1);
    var deletionStarted = new CountDownLatch(1);
    var first = new AtomicBoolean(true);
    when(gateway.get("/agent-groups"))
        .thenAnswer(
            call -> {
              if (first.getAndSet(false)) {
                entered.countDown();
                if (!release.await(5, TimeUnit.SECONDS)) throw new AssertionError("写入未放行");
              }
              return directory;
            });
    ExecutorService pool = Executors.newFixedThreadPool(2);
    try {
      Future<?> assignment = pool.submit(() -> configs.assignGroups("roles", List.of(id), b));
      assertThat(entered.await(5, TimeUnit.SECONDS)).isTrue();
      Future<?> deletion =
          pool.submit(
              () -> {
                deletionStarted.countDown();
                groups.delete(b);
              });
      assertThat(deletionStarted.await(5, TimeUnit.SECONDS)).isTrue();
      assertThatThrownBy(() -> deletion.get(150, TimeUnit.MILLISECONDS))
          .isInstanceOf(TimeoutException.class);
      release.countDown();
      assignment.get(5, TimeUnit.SECONDS);
      assertThatThrownBy(() -> deletion.get(5, TimeUnit.SECONDS))
          .hasCauseInstanceOf(ConflictFailure.class);
      verify(gateway, never()).delete(anyString());
      assertThat(configs.listRoles("批量角色").get(0).path("groupId").asText()).isEqualTo(b);
    } finally {
      release.countDown();
      pool.shutdownNow();
    }
  }
}
