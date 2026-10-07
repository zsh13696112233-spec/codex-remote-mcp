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
  void completionInvitesPlanReviewOnlyForFirstStepWithAvailableDocument() {
    var snapshot = json.createObjectNode().put("advanceMode", "semi_automatic");
    snapshot.putObject("pendingAdvance").put("completedNodeId", "plan").put("nextNodeId", "build");
    var nodes = snapshot.putArray("nodes");
    // 首步身份由依赖确定，不依赖数组位置。
    nodes.addObject().put("id", "build").putArray("dependsOn").add("plan");
    var plan = nodes.addObject().put("id", "plan").put("response", "原始总结");
    plan.putArray("dependsOn");
    var document = plan.putArray("documents").addObject().put("id", "doc");
    assertThat(DingTalkWaitingCard.completion(snapshot))
        .isEqualTo("原始总结\n\n请查看计划文档，如需调整可回复此卡片，确认后点击「继续执行」。");

    document.put("error", "同步失败");
    assertThat(DingTalkWaitingCard.completion(snapshot))
        .contains("请查看本步骤结果")
        .doesNotContain("计划文档");
    document.remove("error");
    document.put("removed", true);
    assertThat(DingTalkWaitingCard.completion(snapshot))
        .contains("请查看本步骤结果")
        .doesNotContain("计划文档");
    document.put("removed", false);
    plan.withArray("dependsOn").add("earlier");
    assertThat(DingTalkWaitingCard.completion(snapshot))
        .contains("请查看本步骤结果")
        .doesNotContain("计划文档");
  }

  @Test
  void completionWithoutDocumentOrResponseStillExplainsReplyAndContinue() {
    var snapshot = json.createObjectNode().put("advanceMode", "semi_automatic");
    snapshot.putObject("pendingAdvance").put("completedNodeId", "plan").put("nextNodeId", "build");
    snapshot.putArray("nodes").addObject().put("id", "plan").put("response", "原始总结");
    assertThat(DingTalkWaitingCard.completion(snapshot))
        .isEqualTo("原始总结\n\n请查看本步骤结果，如需调整可回复此卡片，确认后点击「继续执行」。");
    ((ObjectNode) snapshot.path("nodes").get(0)).put("response", " ");
    assertThat(DingTalkWaitingCard.completion(snapshot))
        .isEqualTo("步骤已完成。请查看本步骤结果，如需调整可回复此卡片，确认后点击「继续执行」。");
    snapshot.remove("nodes");
    assertThat(DingTalkWaitingCard.completion(snapshot))
        .isEqualTo("步骤已完成。请查看本步骤结果，如需调整可回复此卡片，确认后点击「继续执行」。");
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
    node.putArray("documents").addObject().put("id", "doc").put("name", "[方案].md");
    var data = DingTalkWaitingCard.render("flow", payload, snapshot, "http://example.test:8090/");
    assertThat(data).containsEntry("_templateId", "wide.schema").containsEntry("summary", "总结");
    assertThat(data.get("documents").toString())
        .contains("http://example.test:8090/document.html?workflowId=flow&documentId=doc");
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
