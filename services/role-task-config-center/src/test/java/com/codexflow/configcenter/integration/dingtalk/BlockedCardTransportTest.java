package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import java.io.IOException;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.Map;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;

class BlockedCardTransportTest {
  private final HttpClient http = mock(HttpClient.class);
  private final DingTalkProperties properties = new DingTalkProperties();
  private final ObjectMapper json = new ObjectMapper();

  private OfficialDingTalkTransport transport() {
    properties.setClientId("frozen-client");
    properties.setClientSecret("test-placeholder");
    return new OfficialDingTalkTransport(properties, json, http);
  }

  @SuppressWarnings("unchecked")
  private HttpResponse<String> response(int code, String body) {
    HttpResponse<String> response = mock(HttpResponse.class);
    when(response.statusCode()).thenReturn(code);
    when(response.body()).thenReturn(body);
    return response;
  }

  private void send(OfficialDingTalkTransport transport) {
    transport.sendBlockedCard(
        "stable-card",
        "frozen-client",
        "target-group",
        "developer",
        Map.of("_templateId", "dedicated.schema", "body", "阻断"));
  }

  @Test
  void waitingCardIncludesMentionMetadataOnlyForGroupDelivery() {
    var data =
        Map.<String, Object>of(
            "_cardAtUserId",
            "asker",
            "_cardAtDisplayName",
            "夜雨",
            "markdown",
            "回复 <a atId=\"asker\">夜雨</a>");
    var body = transport().waitingCardBody("card", "GROUP", "group", "asker", data);
    assertThat(body.path("cardAtUserIds").get(0).asText()).isEqualTo("asker");
    assertThat(body.path("imGroupOpenDeliverModel").path("atUserIds").path("asker").asText())
        .isEqualTo("夜雨");
    assertThat(body.path("cardData").path("cardParamMap").has("_cardAtUserId")).isFalse();
    var single = transport().waitingCardBody("card", "PERSON", "asker", "asker", data);
    assertThat(single.has("cardAtUserIds")).isFalse();
    assertThat(single.has("imGroupOpenDeliverModel")).isFalse();
  }

  @Test
  void blockedCardContainsPlatformMentionAndMatchingRecipient() {
    var body =
        transport()
            .blockedCardBody(
                "stable-card",
                "target-group",
                "staff-123",
                Map.of("_templateId", "dedicated.schema", "body", "缺少源码", "_atDisplayName", "张三"));
    assertThat(body.path("cardAtUserIds").size()).isEqualTo(1);
    assertThat(body.path("cardAtUserIds").get(0).asText()).isEqualTo("staff-123");
    assertThat(body.path("userIdType").asInt()).isEqualTo(1);
    assertThat(body.path("imGroupOpenDeliverModel").path("atUserIds").path("staff-123").asText())
        .isEqualTo("张三");
    var parameters = body.path("cardData").path("cardParamMap");
    assertThat(parameters.path("body").asText())
        .isEqualTo("缺少源码\n\n请 <a atId=\"staff-123\">张三</a> 查看任务结果。");
    assertThat(parameters.has("_atDisplayName")).isFalse();
    assertThat(parameters.has("_templateId")).isFalse();
  }

  @Test
  void completedCardMentionsRecipientWithoutBlockedWording() {
    var body =
        transport()
            .blockedCardBody(
                "completed-card",
                "target-group",
                "staff-123",
                Map.of(
                    "_templateId",
                    "dedicated.schema",
                    "title",
                    "Jira 自动流程已完成",
                    "body",
                    "Jira：TEST-2\n已完成审核",
                    "_atDisplayName",
                    "张三"));
    var parameters = body.path("cardData").path("cardParamMap");
    assertThat(parameters.path("title").asText()).isEqualTo("Jira 自动流程已完成");
    assertThat(parameters.path("body").asText())
        .contains("查看任务结果", "atId=\"staff-123\"")
        .doesNotContain("阻断");
    assertThat(body.path("imGroupOpenDeliverModel").path("atUserIds").has("staff-123")).isTrue();
  }

  @Test
  void mentionEscapesAttributesAndNamesWithoutMutatingFrozenPayload() {
    var data = Map.<String, Object>of("body", "原正文", "_atDisplayName", "张<三>&\"");
    var body = transport().blockedCardBody("card", "group", "id\"<>&", data);
    assertThat(body.path("cardData").path("cardParamMap").path("body").asText())
        .contains("atId=\"id&quot;&lt;&gt;&amp;\"", "张&lt;三&gt;&amp;&quot;</a>");
    assertThat(body.path("cardAtUserIds").get(0).asText()).isEqualTo("id\"<>&");
    assertThat(data.get("body")).isEqualTo("原正文");
  }

  @Test
  void missingOrAllRecipientCannotSendBlockedCard() {
    var transport = transport();
    for (String person : new String[] {null, "", " ", "@ALL"}) {
      assertThatThrownBy(
              () ->
                  transport.sendBlockedCard(
                      "card", "frozen-client", "group", person, Map.of("body", "阻断")))
          .isInstanceOf(BlockedNotificationService.NotSentFailure.class);
    }
    verifyNoInteractions(http);
  }

  @Test
  void blockedSendCarriesMentionInActualHttpRequest() throws Exception {
    var transport = transport();
    when(http.send(
            any(HttpRequest.class),
            org.mockito.ArgumentMatchers.<HttpResponse.BodyHandler<String>>any()))
        .thenAnswer(invocation -> response(200, "{\"accessToken\":\"test-token\"}"))
        .thenAnswer(
            invocation -> {
              HttpRequest request = invocation.getArgument(0);
              var subscriber =
                  HttpResponse.BodySubscribers.ofString(java.nio.charset.StandardCharsets.UTF_8);
              request
                  .bodyPublisher()
                  .orElseThrow()
                  .subscribe(
                      new java.util.concurrent.Flow.Subscriber<java.nio.ByteBuffer>() {
                        public void onSubscribe(
                            java.util.concurrent.Flow.Subscription subscription) {
                          subscriber.onSubscribe(subscription);
                        }

                        public void onNext(java.nio.ByteBuffer item) {
                          subscriber.onNext(java.util.List.of(item));
                        }

                        public void onError(Throwable error) {
                          subscriber.onError(error);
                        }

                        public void onComplete() {
                          subscriber.onComplete();
                        }
                      });
              var body = json.readTree(subscriber.getBody().toCompletableFuture().get());
              assertThat(body.path("cardAtUserIds").get(0).asText()).isEqualTo("developer");
              assertThat(body.path("cardData").path("cardParamMap").path("body").asText())
                  .contains("<a atId=\"developer\">开发人</a>");
              assertThat(body.path("cardTemplateId").asText()).isEqualTo("dedicated.schema");
              return response(
                  200,
                  "{\"success\":true,\"result\":{\"deliverResults\":[{\"spaceType\":\"IM_GROUP\",\"spaceId\":\"target-group\",\"success\":true}]}}");
            });
    send(transport);
    verify(http, times(2)).send(any(HttpRequest.class), any());
  }

  @Test
  void successRequiresExactTargetDeliveryReceipt() throws Exception {
    var transport = transport();
    when(http.send(
            any(HttpRequest.class),
            org.mockito.ArgumentMatchers.<HttpResponse.BodyHandler<String>>any()))
        .thenAnswer(invocation -> response(200, "{\"accessToken\":\"test-token\"}"))
        .thenAnswer(
            invocation ->
                response(
                    200,
                    "{\"success\":true,\"result\":{\"deliverResults\":[{\"spaceType\":\"IM_GROUP\",\"spaceId\":\"other-group\",\"success\":true}]}}"));
    assertThatThrownBy(() -> send(transport)).isInstanceOf(IllegalStateException.class);
    verify(http, times(2)).send(any(HttpRequest.class), any());
  }

  @Test
  void timeoutAfterRequestIsUnknownWithoutAutomaticRetry() throws Exception {
    var transport = transport();
    when(http.send(
            any(HttpRequest.class),
            org.mockito.ArgumentMatchers.<HttpResponse.BodyHandler<String>>any()))
        .thenAnswer(invocation -> response(200, "{\"accessToken\":\"test-token\"}"))
        .thenThrow(new IOException("lost response"));
    assertThatThrownBy(() -> send(transport))
        .isInstanceOf(IllegalStateException.class)
        .isNotInstanceOf(BlockedNotificationService.NotSentFailure.class);
    verify(http, times(2)).send(any(HttpRequest.class), any());
  }

  @Test
  void explicitRateLimitIsRetryableButServerErrorIsUnknown() throws Exception {
    for (int code : new int[] {429, 500}) {
      reset(http);
      var transport = transport();
      when(http.send(
              any(HttpRequest.class),
              org.mockito.ArgumentMatchers.<HttpResponse.BodyHandler<String>>any()))
          .thenAnswer(invocation -> response(200, "{\"accessToken\":\"test-token\"}"))
          .thenAnswer(invocation -> response(code, "{}"));
      if (code == 429)
        assertThatThrownBy(() -> send(transport))
            .isInstanceOf(BlockedNotificationService.NotSentFailure.class);
      else assertThatThrownBy(() -> send(transport)).isInstanceOf(IllegalStateException.class);
      verify(http, times(2)).send(any(HttpRequest.class), any());
    }
  }

  @Test
  void changedBotCannotSendUnderDifferentIdentity() {
    var transport = transport();
    properties.setClientId("new-client");
    assertThatThrownBy(() -> send(transport))
        .isInstanceOf(BlockedNotificationService.NotSentFailure.class);
    verifyNoInteractions(http);
  }
}
