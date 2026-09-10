package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.io.IOException;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 验证钉钉 Stream 原始消息和卡片回调到内部模型的映射。 */
class DingTalkTransportParsingTest {

  @Test
  void parsesRealCardContentTreeAndKeepsReplyImageSeparate() {
    var message =
        transport.toMessage(
            """
        {"msgId":"m","conversationId":"g","conversationType":"2","msgtype":"richText",
         "content":{"richText":[{"text":"这张图呢"},{"type":"picture","downloadCode":"new-image"}],
          "repliedMsg":{"msgId":"native-id","content":{"cardContent":[
           {"elementType":"text","value":"任务名"},
           {"elementType":"markdown","children":[{"elementType":"text","value":"等待确认"},
            {"elementType":"text","value":"工作流编号：00000000-0000-4000-8000-000000000001"}]}]}}}}
        """);
    assertThat(message.quotedText())
        .isEqualTo("任务名\n等待确认\n工作流编号：00000000-0000-4000-8000-000000000001");
    assertThat(message.quotedCard()).isTrue();
    assertThat(message.withContent("继续").quotedCard()).isTrue();
    assertThat(message.withReference("another").quotedCard()).isTrue();
    assertThat(message.imageCodes()).containsExactly("new-image");
    assertThat(message.content()).isEqualTo("这张图呢");
  }

  @Test
  void quoteDiagnosticsExposeNestedSchemaWithoutMessageOrCredentialValues() {
    var quote = objectMapper.createObjectNode();
    quote.put("msgId", "private-message-id");
    quote
        .putObject("content")
        .put(
            "cardData",
            "{\"cardParamMap\":{\"workflowId\":\"private-workflow-id\",\"markdown\":\"秘密正文\"}}");
    quote.put("sessionWebhook", "https://secret.example/token");
    quote.put("敏感字段内容", "private-value");
    String shape = transport.quotedStructure(quote);
    assertThat(shape)
        .contains("cardData", "embeddedJson", "cardParamMap", "workflowId", "markdown")
        .doesNotContain(
            "private-message-id",
            "private-workflow-id",
            "秘密正文",
            "secret.example",
            "private-value",
            "敏感字段内容");
    var deep = objectMapper.createObjectNode();
    var cursor = deep;
    for (int i = 0; i < 20; i++) cursor = cursor.putObject("nested");
    assertThat(transport.quotedStructure(deep)).contains("已截断");
    assertThat(transport.quotedStructure(objectMapper.createObjectNode())).isEqualTo("{}");
  }

  @Test
  void callbackRetainsExplicitSpaceIdEvenWithoutSpaceType() {
    var action =
        transport.toAction(
            """
        {"outTrackId":"wait-test","spaceId":"wrong-group","userId":"user",
         "content":{"cardPrivateData":{"params":{"action":"advance_confirm"}}}}
        """);
    assertThat(action.conversationId()).isEqualTo("wrong-group");
  }

  @Test
  void officialCardCallbackUsesSpaceIdRatherThanDeliveryOpenSpaceId() {
    var action =
        transport.toAction(
            """
        {"outTrackId":"wait-test","spaceId":"conversation-1","spaceType":"IM_GROUP","userId":"user-2",
         "content":{"cardPrivateData":{"actionIds":["button"],"params":{
           "action":"advance_confirm","workflowId":"workflow-1","gateId":"gate-1"}}}}
        """);
    assertThat(action.conversationId()).isEqualTo("conversation-1");
    assertThat(action.operatorUserId()).isEqualTo("user-2");
    assertThat(action.actionId()).isEqualTo("advance_confirm");
    assertThat(action.value())
        .containsEntry("workflowId", "workflow-1")
        .containsEntry("gateId", "gate-1");
  }

  @Test
  void keepsBothNativeReferenceIdsWhenTopLevelDiffersFromQuotedMessage() {
    var message =
        transport.toMessage(
            """
        {"msgId":"m","conversationId":"g","conversationType":"2","originalMsgId":"top-level",
         "text":{"content":"这个有问题","repliedMsg":{"msgId":"carrier-id","content":{}}}}
        """);
    assertThat(message.referenceIds()).containsExactly("top-level", "carrier-id");
    assertThat(message.withContent("新内容").referenceIds()).isEqualTo(message.referenceIds());
    assertThat(message.withReference("carrier-id").referenceIds()).containsExactly("carrier-id");
    assertThat(DingTalkModels.referenceFingerprint("carrier-id"))
        .matches("10:[a-f0-9]{16}")
        .doesNotContain("carrier-id");
  }

  @Test
  void waitingCardReceiptKeepsCarrierAndRejectsUnsuccessfulDelivery() {
    var response =
        objectMapper.readTree(
            """
        {"success":true,"result":{"deliverResults":[
          {"spaceType":"IM_GROUP","spaceId":"g","success":true,"carrierId":"platform-message"}]}}
        """);
    assertThat(OfficialDingTalkTransport.waitingCardReceipt(response, "GROUP", "g").messageId())
        .isEqualTo("platform-message");
    assertThatThrownBy(
            () -> OfficialDingTalkTransport.waitingCardReceipt(response, "GROUP", "other"))
        .hasMessage("钉钉卡片缺少目标会话投递结果。");
    ((ObjectNode) response.path("result").path("deliverResults").get(0)).put("success", false);
    assertThatThrownBy(() -> OfficialDingTalkTransport.waitingCardReceipt(response, "GROUP", "g"))
        .hasMessage("钉钉卡片投递未成功。");
  }

  @Test
  void explicitQuotedCardIdIsNotHiddenByPlatformMessageId() {
    var message =
        transport.toMessage(
            """
        {"msgId":"m","conversationId":"g","conversationType":"2","originalMsgId":"platform-message",
         "text":{"content":"这个有问题","repliedMsg":{"outTrackId":"wait-outbox","msgId":"platform-message"}}}
        """);
    assertThat(message.replyToMessageId()).isEqualTo("wait-outbox");
    var platformOnly =
        transport.toMessage(
            """
        {"msgId":"m","conversationId":"g","conversationType":"2",
         "text":{"content":"这个有问题","repliedMsg":{"msgId":"platform-message"}}}
        """);
    assertThat(platformOnly.replyToMessageId()).isEqualTo("platform-message");
  }

  @Test
  void acceptsHttpAndHttpsImageUrlsWithoutChangingSignedQuery() {
    for (String scheme : new String[] {"http", "https"}) {
      String address =
          scheme + "://files.example.com/image.png?signature=test%2Bvalue%2Fpart&expires=123";
      var response = objectMapper.createObjectNode().put("downloadUrl", " " + address + " ");
      assertThat(OfficialDingTalkTransport.imageDownloadUri(response).toString())
          .isEqualTo(address);
    }
  }

  @Test
  void invalidImageUrlsHaveSpecificErrorsWithoutLeakingAddress() {
    assertThatThrownBy(
            () -> OfficialDingTalkTransport.imageDownloadUri(objectMapper.createObjectNode()))
        .hasMessage("钉钉未返回图片下载地址，请重新发送图片。");
    for (String address :
        new String[] {
          "https://files.example.com/bad path?signature=secret",
          "https://user:secret@files.example.com/a",
          "https:///a?signature=secret"
        }) {
      assertThatThrownBy(
              () ->
                  OfficialDingTalkTransport.imageDownloadUri(
                      objectMapper.createObjectNode().put("downloadUrl", address)))
          .hasMessage("钉钉图片下载地址格式错误，请重新发送图片。")
          .hasNoCause();
    }
    assertThatThrownBy(
            () ->
                OfficialDingTalkTransport.imageDownloadUri(
                    objectMapper
                        .createObjectNode()
                        .put("downloadUrl", "file:///private/image.png")))
        .hasMessage("钉钉图片下载地址协议不受支持，请重新发送图片。");
  }

  @Test
  void richTextKeepsTextAndOriginalImageOrder() {
    var message =
        transport.toMessage(
            """
        {"msgId":"m", "conversationId":"g", "conversationType":"2", "senderStaffId":"bob",
         "isInAtList":true, "sessionWebhook":"https://oapi.dingtalk.com/robot/sendBySession?session=test",
         "msgtype":"richText", "content":{"richText":[{"text":"编号 问题"},
           {"type":"picture", "downloadCode":"first"}, {"type":"picture", "pictureDownloadCode":"second"}]}}
        """);
    assertThat(message.content()).isEqualTo("编号 问题");
    assertThat(message.imageCodes()).containsExactly("first", "second");
    assertThat(message.senderUserId()).isEqualTo("bob");
    assertThat(message.sessionWebhook()).contains("sendBySession");
  }

  @Test
  void imageSeparatesWorkflowIdFromCaptionWithoutSplittingTextFragments() {
    var message =
        transport.toMessage(
            """
        {"msgId":"m","conversationId":"g","conversationType":"2","senderStaffId":"bob",
         "isInAtList":true,"msgtype":"richText","content":{"richText":[
           {"text":"a8bce1a0-25e7-42ae-8e48-"},{"text":"e3ea79afe455"},
           {"type":"picture","downloadCode":"original"},{"text":"这是你写的吗"}]}}
        """);
    assertThat(message.content()).isEqualTo("a8bce1a0-25e7-42ae-8e48-e3ea79afe455\n这是你写的吗");
    assertThat(message.imageCodes()).containsExactly("original");
  }

  @Test
  void pictureMessageWithoutTextRetainsItsDownloadCode() {
    var message =
        transport.toMessage(
            """
        {"msgId":"m", "conversationId":"g", "conversationType":"1", "senderStaffId":"bob",
         "msgtype":"picture", "content":{"downloadCode":"original"}}
        """);
    assertThat(message.content()).isEmpty();
    assertThat(message.imageCodes()).containsExactly("original");
  }

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
  void parsesNestedQuoteWithoutMixingQuotedTextIntoQuestion() {
    for (boolean embedded : new boolean[] {false, true}) {
      ObjectNode raw = objectMapper.createObjectNode();
      raw.put("msgId", "question").put("conversationId", "group").put("msgtype", "text");
      ObjectNode text = raw.putObject("text").put("content", "是谁创建的").put("isReplyMsg", true);
      ObjectNode quote = objectMapper.createObjectNode().put("msgId", "bot-message");
      quote.putObject("content").put("text", "工作流编号：测试编号\n步骤「策划」");
      if (embedded) text.put("repliedMsg", quote.toString());
      else text.set("repliedMsg", quote);

      var message = transport.toMessage(raw.toString());
      assertThat(message.replyToMessageId()).isEqualTo("bot-message");
      assertThat(message.quotedText()).isEqualTo("工作流编号：测试编号\n步骤「策划」");
      assertThat(message.content()).isEqualTo("是谁创建的");
      assertThat(message.imageCodes()).isEmpty();
    }
  }

  @Test
  void richTextReplyRetainsQuoteImageAndCaption() {
    for (boolean embedded : new boolean[] {false, true}) {
      var raw = objectMapper.createObjectNode().put("msgtype", "richText");
      var content = objectMapper.createObjectNode().put("isReplyMsg", true);
      var quote = content.putObject("repliedMsg").put("msgId", "task-message");
      quote.putObject("content").put("text", "工作流编号：原任务\n步骤「策划」");
      var items = content.putArray("richText");
      items.addObject().put("type", "picture").put("downloadCode", "new-image");
      items.addObject().put("text", "怎么有这么多文件");
      if (embedded) raw.put("content", content.toString());
      else raw.set("content", content);

      var message = transport.toMessage(raw.toString());
      assertThat(message.replyToMessageId()).isEqualTo("task-message");
      assertThat(message.quotedText()).isEqualTo("工作流编号：原任务\n步骤「策划」");
      assertThat(message.content()).isEqualTo("怎么有这么多文件");
      assertThat(message.imageCodes()).containsExactly("new-image");
    }
  }

  @Test
  void parsesQuoteTextShapesAndKeepsQuotedImagesSeparate() {
    for (String body :
        new String[] {
          "{\"text\":{\"content\":\"任务结果\"}}",
          "{\"content\":\"任务结果\"}",
          "{\"content\":{\"text\":\"任务结果\"}}",
          "{\"content\":{\"content\":\"任务结果\"}}",
          "{\"content\":{\"richText\":[{\"text\":\"任务\"},{\"text\":\"结果\"}]}}"
        }) {
      ObjectNode raw = objectMapper.createObjectNode();
      raw.putObject("text").put("content", "问题");
      raw.set("repliedMsg", objectMapper.readTree(body));
      assertThat(transport.toMessage(raw.toString()).quotedText()).isEqualTo("任务结果");
    }
    var message =
        transport.toMessage(
            """
        {"text":{"content":"问题","repliedMsg":{"msgId":"picture-message",
          "content":{"downloadCode":"quoted-image"}}}}
        """);
    assertThat(message.replyToMessageId()).isEqualTo("picture-message");
    assertThat(message.quotedText()).isEmpty();
    assertThat(message.imageCodes()).isEmpty();
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

  @Test
  void parsesChildDepartmentIdsFromNestedResult() throws Exception {
    assertThat(
            OfficialDingTalkTransport.childDepartmentIds(
                objectMapper.readTree(
                    """
                    {"errcode":0,"errmsg":"ok","result":{"dept_id_list":[2,3,4]}}
                    """)))
        .isEqualTo(objectMapper.readTree("[2,3,4]"));
  }

  @Test
  void retriesOnlyFailuresCausedByIoErrors() {
    assertThat(
            OfficialDingTalkTransport.causedByIOException(
                new IllegalStateException("request failed", new IOException("GOAWAY"))))
        .isTrue();
    assertThat(
            OfficialDingTalkTransport.causedByIOException(
                new IllegalStateException("invalid response")))
        .isFalse();
  }
}
