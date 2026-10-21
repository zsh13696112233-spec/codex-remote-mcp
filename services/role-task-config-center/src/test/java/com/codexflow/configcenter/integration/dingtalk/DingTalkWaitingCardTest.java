package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Instant;
import java.util.Map;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

class DingTalkWaitingCardTest {
  private final ObjectMapper json = new ObjectMapper();

  @Test
  void monitorLinkSurvivesAllWaitStatesAndBothTemplates() {
    for (String kind : new String[] {"advance", "blocked", "finish"}) {
      for (boolean answer : new boolean[] {false, true}) {
        for (String template : new String[] {"", "wide-template"}) {
          for (String state : new String[] {"held", "busy", "closed"}) {
            var payload =
                json.createObjectNode()
                    .put("gateId", "gate")
                    .put("waitKind", kind)
                    .put("actionFirst", true)
                    .put("answer", answer)
                    .put("documentTemplateId", template)
                    .put("text", "步骤结果");
            var snapshot = json.createObjectNode();
            snapshot
                .putObject("pendingAdvance")
                .put("gateId", "gate")
                .put("kind", kind)
                .put("state", state);
            var data =
                DingTalkWaitingCard.render("flow", payload, snapshot, "http://192.0.2.1:8090/");
            for (String field : new String[] {"markdown", "notice"}) {
              assertThat(data.get(field).toString())
                  .containsOnlyOnce("[查看监控](http://192.0.2.1:8090/?workflowId=flow)");
            }
            snapshot
                .putArray("nodes")
                .addObject()
                .put("id", "step")
                .putArray("documents")
                .addObject()
                .put("id", "doc")
                .put("available", true);
            payload.put("documentNodeId", "step");
            var refreshed =
                DingTalkWaitingCard.render("flow", payload, snapshot, "http://192.0.2.1:8090/");
            assertThat(refreshed.get("notice").toString()).containsOnlyOnce("[查看监控]");
            assertThat(refreshed.get("documents").toString()).contains("查看文档");
          }
        }
      }
    }
  }

  @Test
  void questionContextIsFirstEscapedAndRetainedWhenClosed() {
    var payload =
        json.createObjectNode()
            .put("gateId", "gate")
            .put("answer", true)
            .put("cardMention", true)
            .put("mentionGroup", true)
            .put("atUserId", "asker")
            .put("atDisplayName", "夜雨")
            .put("question", "第一行\n<a atId=\"bad\">假提及</a> [链接](https://example.com)")
            .put("questionHasImages", true)
            .put("text", "回答");
    var snapshot = json.createObjectNode();
    snapshot.putObject("pendingAdvance").put("gateId", "gate").put("state", "held");
    var active = DingTalkWaitingCard.render("workflow", payload, snapshot);
    for (String field : new String[] {"markdown", "statusText"}) {
      assertThat(active.get(field).toString())
          .startsWith("**回复 <a atId=\"asker\">夜雨</a>**")
          .contains("你的问题", "第一行", "（附图片）", "&lt;a")
          .doesNotContain("<a atId=\"bad\">");
    }
    snapshot.remove("pendingAdvance");
    var closed = DingTalkWaitingCard.render("workflow", payload, snapshot);
    assertThat(closed.get("statusText").toString()).contains("你的问题", "本轮等待已结束");
    assertThat(closed.get("_cardAtUserId")).isEqualTo("asker");
    payload.put("mentionGroup", false);
    var single = DingTalkWaitingCard.render("workflow", payload, snapshot);
    assertThat(single.get("statusText").toString()).startsWith("**回复 夜雨**");
    assertThat(single).doesNotContainKey("_cardAtUserId");
  }

  @Test
  void resultContextUsesActionWithoutInventingQuestion() {
    var payload =
        json.createObjectNode()
            .put("gateId", "gate")
            .put("cardMention", true)
            .put("mentionGroup", true)
            .put("atUserId", "operator")
            .put("atDisplayName", "操作人")
            .put("resultAction", "advance_retry");
    var result = DingTalkWaitingCard.render("workflow", payload, json.createObjectNode());
    assertThat(result.get("markdown").toString())
        .startsWith("<a atId=\"operator\">操作人</a>，本步骤重新执行已有结果。")
        .doesNotContain("你的问题");
    payload.put("resultAction", "advance_confirm");
    assertThat(
            DingTalkWaitingCard.render("workflow", payload, json.createObjectNode())
                .get("statusText")
                .toString())
        .contains("你确认执行的步骤已有结果");
  }

  @Test
  void policyCardsDistinguishRetryFinalConfirmationAndStop() {
    for (String kind : new String[] {"advance", "blocked", "finish"}) {
      var snapshot = json.createObjectNode();
      var gate =
          snapshot
              .putObject("pendingAdvance")
              .put("gateId", "gate")
              .put("kind", kind)
              .put("state", "countdown")
              .put("expiresAt", Instant.now().plusSeconds(300).toString());
      var payload = json.createObjectNode().put("gateId", "gate").put("waitKind", kind);
      var data = DingTalkWaitingCard.render("workflow", payload, snapshot);
      assertThat(data.get("confirmLabel"))
          .isEqualTo(kind.equals("blocked") ? "重新执行" : kind.equals("finish") ? "确认结束" : "进入下一步");
      assertThat(data.get("confirmAction"))
          .isEqualTo(kind.equals("blocked") ? "advance_retry" : "advance_confirm");
      assertThat(data.get("showStop")).isEqualTo("true");
      assertThat(data.get("notice").toString()).contains("北京时间").contains("无人回复");
      gate.put("state", "held").put("expiresAt", "2000-01-01T00:00:00Z");
      assertThat(DingTalkWaitingCard.render("workflow", payload, snapshot).get("confirmStatus"))
          .isEqualTo("normal");
      assertThat(DingTalkWaitingCard.render("workflow", payload, snapshot).get("notice").toString())
          .contains("等待人工处理", "不会自动流转或结束")
          .doesNotContain("倒计时");
      gate.put("state", "busy");
      var busy = DingTalkWaitingCard.render("workflow", payload, snapshot);
      assertThat(busy.get("confirmStatus")).isEqualTo("disabled");
      assertThat(busy.get("stopStatus")).isEqualTo("disabled");
      gate.put("gateId", "new");
      assertThat(DingTalkWaitingCard.render("workflow", payload, snapshot).get("stopStatus"))
          .isEqualTo("disabled");
    }
  }

  @Test
  void focusPrecedesDetailsAndActionsFollowCurrentState() {
    for (String kind : new String[] {"blocked", "advance", "finish"}) {
      var snapshot = json.createObjectNode();
      var gate =
          snapshot
              .putObject("pendingAdvance")
              .put("gateId", "gate")
              .put("kind", kind)
              .put("state", "held");
      var payload =
          json.createObjectNode()
              .put("gateId", "gate")
              .put("waitKind", kind)
              .put("actionFirst", true)
              .put("text", "完整执行记录")
              .put("blockedReason", kind.equals("blocked") ? "请提供测试角色" : "");
      String label = kind.equals("blocked") ? "重新执行" : kind.equals("finish") ? "确认结束" : "进入下一步";
      var data = DingTalkWaitingCard.render("workflow", payload, snapshot);
      for (String field : new String[] {"summary", "markdown"}) {
        String text = data.get(field).toString();
        assertThat(text.indexOf("点击「" + label)).isLessThan(text.indexOf("完整执行记录"));
        assertThat(text).containsOnlyOnce("当前操作：");
        if (kind.equals("blocked"))
          assertThat(text.indexOf("请提供测试角色")).isLessThan(text.indexOf("当前操作："));
      }
      assertThat(data.get("notice").toString())
          .contains("[查看监控](http://127.0.0.1:8090/?workflowId=workflow)");
      gate.put("state", "busy");
      var busy = DingTalkWaitingCard.render("workflow", payload, snapshot);
      assertThat(busy.get("summary").toString()).contains("正在处理", "完整执行记录").doesNotContain("点击「");
      assertThat(busy.get("confirmStatus")).isEqualTo("disabled");
      gate.put("gateId", "new-gate");
      var closed = DingTalkWaitingCard.render("workflow", payload, snapshot);
      assertThat(closed.get("summary").toString())
          .contains("本轮等待已结束", "完整执行记录")
          .doesNotContain("点击「");
      assertThat(closed.get("confirmStatus")).isEqualTo("disabled");
      payload.put("answer", true);
      assertThat(
              DingTalkWaitingCard.render("workflow", payload, snapshot).get("summary").toString())
          .contains("本次回答");
    }
  }

  @Test
  void blockedReasonIsSeparateFilteredAndOnlyUsedForBlockedWaits() {
    var snapshot = json.createObjectNode();
    var gate =
        snapshot.putObject("pendingAdvance").put("completedNodeId", "plan").put("kind", "blocked");
    var node =
        snapshot
            .putArray("nodes")
            .addObject()
            .put("id", "plan")
            .put("response", "完整总结")
            .put("error", "请核对权限 token=test-secret http://127.0.0.1:8080 thread_private");
    assertThat(DingTalkWaitingCard.completion(snapshot)).isEqualTo("完整总结");
    assertThat(DingTalkWaitingCard.blockedReason(snapshot))
        .contains("请核对权限")
        .doesNotContain("test-secret", "127.0.0.1", "thread_private");
    node.putNull("error");
    assertThat(DingTalkWaitingCard.blockedReason(snapshot)).isEmpty();
    node.put("response", " ");
    assertThat(DingTalkWaitingCard.completion(snapshot)).isEqualTo("当前步骤尚未完成。");
    node.put("error", "旧错误");
    gate.put("kind", "advance");
    assertThat(DingTalkWaitingCard.blockedReason(snapshot)).isEmpty();
    assertThat(DingTalkWaitingCard.completion(snapshot)).isEqualTo("步骤已完成。");
  }

  @Test
  void documentLinksUseFrozenNodeAndConfiguredTemplate() {
    var payload =
        json.createObjectNode()
            .put("gateId", "gate")
            .put("documentNodeId", "plan")
            .put("documentTemplateId", "wide.schema")
            .put("text", "总结");
    var snapshot = json.createObjectNode();
    snapshot.putObject("pendingAdvance").put("gateId", "gate").put("state", "held");
    var node = snapshot.putArray("nodes").addObject().put("id", "plan").put("resultRevision", 4);
    var docs = node.putArray("documents");
    docs.addObject()
        .put("id", "doc")
        .put("name", "[方案].md")
        .put("primary", true)
        .put("available", true);
    docs.addObject()
        .put("id", "summary")
        .put("name", "交接总结")
        .put("primary", false)
        .put("available", true);
    docs.addObject()
        .put("id", "missing")
        .put("name", "未同步文档")
        .put("available", false)
        .put("error", "未同步");
    var data = DingTalkWaitingCard.render("flow", payload, snapshot, "http://example.test:8090/");
    assertThat(data).containsEntry("_templateId", "wide.schema").containsEntry("summary", "总结");
    assertThat(data.get("documents").toString())
        .contains(
            "【主文档】",
            "交接总结",
            "未同步文档",
            "documentId=summary",
            "http://example.test:8090/document.html?workflowId=flow&documentId=doc")
        .doesNotContain("documentId=missing");
    assertThat(DingTalkWaitingCard.documentRevision(snapshot, payload)).isEqualTo(4);
    payload.remove("documentTemplateId");
    assertThat(DingTalkWaitingCard.render("flow", payload, snapshot).get("markdown").toString())
        .contains("交付文档", "查看文档");
  }

  @Test
  void replyHistorySurvivesHeldAndClosedStatesAndOldGateCannotContinue() {
    var payload =
        json.createObjectNode().put("gateId", "old").put("text", "完整回答" + "内容".repeat(1000));
    var snapshot = json.createObjectNode();
    var gate = snapshot.putObject("pendingAdvance").put("gateId", "old").put("state", "held");
    assertThat(DingTalkWaitingCard.render("workflow", payload, snapshot))
        .containsEntry("confirmStatus", "normal");
    gate.put("gateId", "new");
    var closed = DingTalkWaitingCard.render("workflow", payload, snapshot);
    assertThat(closed).containsEntry("confirmStatus", "disabled");
    assertThat(closed.get("markdown").toString())
        .contains(payload.path("text").asText(), "本轮等待已结束");
  }

  @Test
  void expiredCountdownIsDisabledAndStepsUseProtocolIds() {
    var snapshot = json.createObjectNode();
    snapshot
        .putObject("pendingAdvance")
        .put("gateId", "gate")
        .put("state", "countdown")
        .put("completedNodeId", "first")
        .put("nextNodeId", "second")
        .put("expiresAt", Instant.now().minusSeconds(1).toString());
    var nodes = snapshot.putArray("nodes");
    nodes.addObject().put("id", "first").put("displayName", "策划");
    nodes.addObject().put("id", "second").put("displayName", "开发");
    assertThat(DingTalkWaitingCard.state(snapshot, "gate")).isEqualTo("closed");
    assertThat(DingTalkWaitingCard.steps(snapshot)).contains("已完成：第1步「策划」", "下一步：第2步「开发」");
  }

  @Test
  void retiredControlCardsAreDisabled() {
    var payload = json.createObjectNode().put("restartControl", true).put("actionId", "old");
    var data = DingTalkWaitingCard.render("workflow", payload, json.createObjectNode());
    assertThat(data)
        .containsEntry("stopMode", "false")
        .containsEntry("restartMode", "false")
        .containsEntry("confirmStatus", "disabled");
  }

  @Test
  void discussionBlocksContinueUntilSaved() {
    var snapshot = json.createObjectNode().put("discussionBusy", true);
    snapshot.putObject("pendingAdvance").put("gateId", "gate").put("state", "held");
    var payload = json.createObjectNode().put("gateId", "gate");
    assertThat(DingTalkWaitingCard.render("workflow", payload, snapshot))
        .containsEntry("confirmStatus", "disabled");
    snapshot.put("discussionBusy", false);
    assertThat(DingTalkWaitingCard.render("workflow", payload, snapshot))
        .containsEntry("confirmStatus", "normal");
  }

  @Test
  void officialDeliveryUsesPublishedTemplateGroupMentionAndPersonalSpace() {
    var transport = new OfficialDingTalkTransport(new DingTalkProperties(), json);
    ObjectNode group =
        transport.waitingCardBody("stable-id", "GROUP", "group", "staff", Map.of("markdown", "回答"));
    assertThat(group.path("cardTemplateId").asText()).isEqualTo(DingTalkWaitingCard.TEMPLATE_ID);
    assertThat(group.path("outTrackId").asText()).isEqualTo("stable-id");
    assertThat(group.path("imGroupOpenDeliverModel").path("atUserIds").has("staff")).isTrue();
    assertThat(group.path("userIdType").asInt()).isEqualTo(1);
    var person = transport.waitingCardBody("stable-id", "PERSON", "staff", "staff", Map.of());
    assertThat(person.path("openSpaceId").asText()).isEqualTo("dtv1.card//IM_ROBOT.staff");
    assertThat(person.has("imGroupOpenDeliverModel")).isFalse();
  }

  @Test
  void quotedCardIdentifierIsPreservedAlongsideRichTextImages() {
    var transport = new OfficialDingTalkTransport(new DingTalkProperties(), json);
    var message =
        transport.toMessage(
            """
      {"msgId":"question","conversationId":"group","conversationType":"2","senderStaffId":"user",
       "msgtype":"richText","content":{"repliedMsg":{"outTrackId":"wait-card"},
       "richText":[{"text":"图片有问题"},{"type":"picture","downloadCode":"image"}]}}
      """);
    assertThat(message.replyToMessageId()).isEqualTo("wait-card");
    assertThat(message.imageCodes()).containsExactly("image");
    assertThat(message.content()).isEqualTo("图片有问题");
  }
}
