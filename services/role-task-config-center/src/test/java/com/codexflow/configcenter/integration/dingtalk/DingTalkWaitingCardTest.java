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
