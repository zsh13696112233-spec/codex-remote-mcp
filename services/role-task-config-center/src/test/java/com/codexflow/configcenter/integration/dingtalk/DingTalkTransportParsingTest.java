package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 验证钉钉 Stream 原始消息和卡片回调到内部模型的映射。 */
class DingTalkTransportParsingTest {

  private final ObjectMapper objectMapper = new ObjectMapper();
  private final OfficialDingTalkTransport transport =
      new OfficialDingTalkTransport(new DingTalkProperties(), objectMapper);

  @Test
  void parsesQuotedGroupMessage() {
    String raw =
        """
        {
          "msgId":"message-1",
          "conversationId":"conversation-1",
          "conversationType":"2",
          "senderStaffId":"user-1",
          "isInAtList":true,
          "originalMsgId":"root-1",
          "text":{"content":"  查询进度  "}
        }
        """;

    DingTalkModels.Message message = transport.toMessage(raw);

    assertThat(message.messageId()).isEqualTo("message-1");
    assertThat(message.conversationId()).isEqualTo("conversation-1");
    assertThat(message.conversationType()).isEqualTo("2");
    assertThat(message.senderUserId()).isEqualTo("user-1");
    assertThat(message.content()).isEqualTo("查询进度");
    assertThat(message.mentionedBot()).isTrue();
    assertThat(message.replyToMessageId()).isEqualTo("root-1");
  }

  @Test
  void parsesDirectPersonMessageAndGroupTitle() {
    DingTalkModels.Message person =
        transport.toMessage(
            """
            {"msgId":"person-message","conversationId":"person-chat","conversationType":"1","senderStaffId":"user-9","text":{"content":"运行"}}
            """);
    DingTalkModels.Message group =
        transport.toMessage(
            """
            {"msgId":"group-message","conversationId":"group-9","conversationType":"2","senderStaffId":"user-9","conversationTitle":"研发群","isInAtList":true,"text":{"content":"运行"}}
            """);

    assertThat(person.senderUserId()).isEqualTo("user-9");
    assertThat(person.conversationType()).isEqualTo("1");
    assertThat(group.conversationTitle()).isEqualTo("研发群");
  }

  @Test
  void parsesEmbeddedQuotedMessageAndMentionAll() {
    String raw =
        """
        {
          "msgId":"message-2",
          "conversationId":"conversation-1",
          "conversationType":"2",
          "senderId":"user-2",
          "isInAtList":true,
          "isAtAll":true,
          "repliedMsg":"{\\\"msgId\\\":\\\"bot-message-1\\\"}",
          "text":{"content":"继续"}
        }
        """;

    DingTalkModels.Message message = transport.toMessage(raw);

    assertThat(message.replyToMessageId()).isEqualTo("bot-message-1");
    assertThat(message.mentionAll()).isTrue();
  }

  @Test
  void parsesCardActionParametersAndConversation() {
    String raw =
        """
        {
          "outTrackId":"card-1",
          "openSpaceId":"dtv1.card//IM_GROUP.conversation-1",
          "userId":"user-2",
          "content":"{\\\"cardPrivateData\\\":{\\\"actionId\\\":\\\"advance_confirm\\\",\\\"params\\\":{\\\"action\\\":\\\"advance_confirm\\\",\\\"workflowId\\\":\\\"workflow-1\\\",\\\"gateId\\\":\\\"gate-1\\\"}}}"
        }
        """;

    DingTalkModels.CardAction action = transport.toAction(raw);

    assertThat(action.cardInstanceId()).isEqualTo("card-1");
    assertThat(action.conversationId()).isEqualTo("conversation-1");
    assertThat(action.operatorUserId()).isEqualTo("user-2");
    assertThat(action.actionId()).isEqualTo("advance_confirm");
    assertThat(action.value()).containsEntry("workflowId", "workflow-1");
  }

  @Test
  void buildsBuiltInMarkdownMessageWithoutCardTemplate() throws Exception {
    ObjectNode body =
        transport.groupMessageBody(
            "conversation-1",
            "sampleMarkdown",
            objectMapper.createObjectNode().put("title", "任务进度").put("text", "**状态：** 运行中"));

    assertThat(body.path("msgKey").asText()).isEqualTo("sampleMarkdown");
    assertThat(body.has("cardTemplateId")).isFalse();
    assertThat(objectMapper.readTree(body.path("msgParam").asText()))
        .isEqualTo(objectMapper.createObjectNode().put("title", "任务进度").put("text", "**状态：** 运行中"));
  }
}
