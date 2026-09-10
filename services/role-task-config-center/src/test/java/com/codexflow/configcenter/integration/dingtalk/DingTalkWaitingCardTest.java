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
  void stopCardDoesNotShowContinueOrRestart() {
    var payload =
        json.createObjectNode()
            .put("restartControl", true)
            .put("controlType", "stop")
            .put("actionId", "stop-action")
            .put("text", "准备停止整个任务。");
    var snapshot = json.createObjectNode();
    snapshot
        .putObject("pendingControl")
        .put("actionId", "stop-action")
        .put("type", "stop")
        .put("status", "pending")
        .put("expiresAt", Instant.now().plusSeconds(600).toString());
    var data = DingTalkWaitingCard.render("workflow", payload, snapshot);
    assertThat(data)
        .containsEntry("stopMode", "true")
        .containsEntry("restartMode", "false")
        .containsEntry("waitingMode", "false")
        .containsEntry("confirmStatus", "normal");
    assertThat(data.get("markdown").toString())
        .contains("确认停止", "取消停止")
        .doesNotContain("返工", "继续执行");
  }

  @Test
  void restartCardUsesControlLifetimeInsteadOfWaitingGate() {
    var payload =
        json.createObjectNode()
            .put("restartControl", true)
            .put("actionId", "action")
            .put("text", "重跑策划。如要继续，请另发一条仅包含“确认执行”的消息；10分钟内有效。")
            .put("controlExpiresAt", "2026-09-10T18:00:00Z");
    var snapshot = json.createObjectNode();
    var control =
        snapshot
            .putObject("pendingControl")
            .put("actionId", "action")
            .put("type", "restart_from")
            .put("status", "pending")
            .put("expiresAt", Instant.now().plusSeconds(600).toString());
    var rendered = DingTalkWaitingCard.render("workflow", payload, snapshot);
    assertThat(rendered)
        .containsEntry("restartMode", "true")
        .containsEntry("waitingMode", "false")
        .containsEntry("confirmStatus", "normal")
        .containsEntry("controlId", "action");
    assertThat(rendered.get("markdown").toString())
        .contains("取消返工", "重跑策划")
        .doesNotContain("另发", "点击“继续执行”", "两分钟");
    control.put("actionId", "replacement");
    assertThat(DingTalkWaitingCard.render("workflow", payload, snapshot))
        .containsEntry("confirmStatus", "disabled");
    control.put("actionId", "action").put("expiresAt", Instant.now().minusSeconds(1).toString());
    assertThat(DingTalkWaitingCard.cardState(snapshot, payload)).isEqualTo("closed");
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
