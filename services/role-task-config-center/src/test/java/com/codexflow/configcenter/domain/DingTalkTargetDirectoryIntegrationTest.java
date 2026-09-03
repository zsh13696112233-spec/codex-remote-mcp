package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

/** 验证钉钉人员同步、群发现以及管理员启用规则。 */
@SpringBootTest
class DingTalkTargetDirectoryIntegrationTest {

  @Autowired DingTalkTargetDirectory targets;

  @Test
  void syncsPeopleAndMarksMissingPeopleUnavailable() {
    String clientId = "directory-" + UUID.randomUUID();
    DingTalkTargetDirectory.SyncResult first =
        targets.syncPeople(
            clientId,
            List.of(
                new DingTalkTargetDirectory.RemotePerson("user-1", "张三", "研发部"),
                new DingTalkTargetDirectory.RemotePerson("user-2", "李四", "产品部")));

    assertThat(first.created()).isEqualTo(2);
    DingTalkTargetDirectory.TargetView user1 =
        targets.list(clientId).stream()
            .filter(item -> item.externalId().equals("user-1"))
            .findFirst()
            .orElseThrow();
    assertThat(user1.enabled()).isFalse();
    assertThat(targets.update(clientId, user1.id(), "张三（负责人）", true).enabled()).isTrue();

    DingTalkTargetDirectory.SyncResult second =
        targets.syncPeople(
            clientId, List.of(new DingTalkTargetDirectory.RemotePerson("user-2", "李四", "产品部")));

    assertThat(second.unavailable()).isEqualTo(1);
    assertThat(targets.list(clientId).stream().filter(item -> item.id().equals(user1.id())))
        .singleElement()
        .satisfies(
            item -> {
              assertThat(item.available()).isFalse();
              assertThat(item.enabled()).isFalse();
            });
  }

  @Test
  void discoveredGroupRequiresAdministratorEnablement() {
    String clientId = "groups-" + UUID.randomUUID();

    DingTalkTargetDirectory.TargetView discovered =
        targets.discoverGroup(clientId, "conversation-1", "质量保障群");

    assertThat(discovered.targetType()).isEqualTo("GROUP");
    assertThat(discovered.displayName()).isEqualTo("质量保障群");
    assertThat(discovered.enabled()).isFalse();
    assertThat(targets.update(clientId, discovered.id(), "质量保障群", true).enabled()).isTrue();
  }
}
