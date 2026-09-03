package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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

  @Test
  void storesDepartmentHierarchyAndPersonMemberships() {
    String clientId = "tree-" + UUID.randomUUID();

    targets.syncDirectory(
        clientId,
        new DingTalkTargetDirectory.RemoteDirectory(
            List.of(
                new DingTalkTargetDirectory.RemoteDepartment("1", null, "根部门"),
                new DingTalkTargetDirectory.RemoteDepartment("2", "1", "研发中心"),
                new DingTalkTargetDirectory.RemoteDepartment("3", "2", "平台组")),
            List.of(
                new DingTalkTargetDirectory.RemotePerson("user-tree", "王五", "平台组", List.of("3")))));

    DingTalkTargetDirectory.DirectoryView directory = targets.directory(clientId);

    assertThat(directory.departments())
        .extracting(
            DingTalkTargetDirectory.DepartmentView::externalId,
            DingTalkTargetDirectory.DepartmentView::parentExternalId,
            DingTalkTargetDirectory.DepartmentView::displayName)
        .containsExactlyInAnyOrder(
            org.assertj.core.groups.Tuple.tuple("1", null, "根部门"),
            org.assertj.core.groups.Tuple.tuple("2", "1", "研发中心"),
            org.assertj.core.groups.Tuple.tuple("3", "2", "平台组"));
    assertThat(directory.people())
        .singleElement()
        .satisfies(person -> assertThat(person.departmentIds()).containsExactly("3"));
  }

  @Test
  void rejectsDeletingDirectoryPerson() {
    String clientId = "person-delete-" + UUID.randomUUID();
    targets.syncPeople(
        clientId, List.of(new DingTalkTargetDirectory.RemotePerson("user-delete", "赵六", "行政部")));
    DingTalkTargetDirectory.TargetView person = targets.list(clientId).get(0);

    assertThatThrownBy(() -> targets.delete(clientId, person.id()))
        .isInstanceOf(ConflictFailure.class)
        .hasMessageContaining("通讯录同步维护");
  }
}
