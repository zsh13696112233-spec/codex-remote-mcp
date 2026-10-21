package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.Flow;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

class DingTalkMonitorTransportTest {
  private final ObjectMapper json = new ObjectMapper();

  @Test
  void proactiveCardsUseGroupAndPersonEndpointsWithCompleteLink() throws Exception {
    for (String targetType : List.of("GROUP", "PERSON")) {
      var http = mock(HttpClient.class);
      var properties = new DingTalkProperties();
      properties.setClientId("test-app");
      properties.setClientSecret("test-placeholder");
      var transport = new OfficialDingTalkTransport(properties, json, http);
      when(http.send(
              any(HttpRequest.class),
              org.mockito.ArgumentMatchers.<HttpResponse.BodyHandler<String>>any()))
          .thenAnswer(call -> response("{\"accessToken\":\"test-token\",\"expireIn\":7200}"))
          .thenAnswer(
              call -> {
                HttpRequest request = call.getArgument(0);
                var body = requestBody(request);
                assertThat(request.uri().getPath())
                    .isEqualTo(
                        "GROUP".equals(targetType)
                            ? "/v1.0/robot/groupMessages/send"
                            : "/v1.0/robot/oToMessages/batchSend");
                assertThat(
                        "GROUP".equals(targetType)
                            ? body.path("openConversationId").asText()
                            : body.path("userIds").get(0).asText())
                    .isEqualTo("recipient");
                assertThat(body.path("msgKey").asText()).isEqualTo("sampleActionCard");
                var card = json.readTree(body.path("msgParam").asText());
                assertThat(card.path("singleTitle").asText()).isEqualTo("查看监控");
                assertThat(card.path("singleURL").asText())
                    .isEqualTo("http://monitor:8090/?workflowId=run");
                assertThat(card.path("text").asText()).contains("工作流编号：run");
                return response("{\"msgId\":\"sent-reference\"}");
              });
      var result =
          transport.sendMonitorCard(
              "recipient",
              targetType,
              json.createObjectNode()
                  .put("text", "工作流编号：run\n\n执行失败")
                  .put("monitorUrl", "http://monitor:8090/?workflowId=run"));
      assertThat(result.messageId()).isEqualTo("sent-reference");
      verify(http, times(2)).send(any(HttpRequest.class), any());
    }
  }

  @Test
  void waitingCardRefreshKeepsMentionTextWithoutRedeliveringNotification() throws Exception {
    var http = mock(HttpClient.class);
    var properties = new DingTalkProperties();
    properties.setClientId("test-app");
    properties.setClientSecret("test-placeholder");
    var transport = new OfficialDingTalkTransport(properties, json, http);
    when(http.send(
            any(HttpRequest.class),
            org.mockito.ArgumentMatchers.<HttpResponse.BodyHandler<String>>any()))
        .thenAnswer(call -> response("{\"accessToken\":\"test-token\",\"expireIn\":7200}"))
        .thenAnswer(
            call -> {
              HttpRequest request = call.getArgument(0);
              assertThat(request.method()).isEqualTo("PUT");
              assertThat(request.uri().getPath()).isEqualTo("/v1.0/card/instances");
              var body = requestBody(request);
              assertThat(body.has("cardAtUserIds")).isFalse();
              assertThat(body.has("imGroupOpenDeliverModel")).isFalse();
              var params = body.path("cardData").path("cardParamMap");
              assertThat(params.path("statusText").asText()).contains("<a atId=\"asker\">夜雨</a>");
              assertThat(params.has("_cardAtUserId")).isFalse();
              assertThat(params.has("_cardAtDisplayName")).isFalse();
              assertThat(params.has("_templateId")).isFalse();
              return response("{}");
            });
    transport.updateCard(
        "card",
        java.util.Map.of(
            "statusText",
            "回复 <a atId=\"asker\">夜雨</a>\n本轮等待已结束",
            "_cardAtUserId",
            "asker",
            "_cardAtDisplayName",
            "夜雨",
            "_templateId",
            "template"));
    verify(http, times(2)).send(any(HttpRequest.class), any());
  }

  @SuppressWarnings("unchecked")
  private HttpResponse<String> response(String body) {
    HttpResponse<String> response = mock(HttpResponse.class);
    when(response.statusCode()).thenReturn(200);
    when(response.body()).thenReturn(body);
    return response;
  }

  private JsonNode requestBody(HttpRequest request) throws Exception {
    var subscriber = HttpResponse.BodySubscribers.ofString(StandardCharsets.UTF_8);
    request
        .bodyPublisher()
        .orElseThrow()
        .subscribe(
            new Flow.Subscriber<ByteBuffer>() {
              public void onSubscribe(Flow.Subscription subscription) {
                subscriber.onSubscribe(subscription);
              }

              public void onNext(ByteBuffer item) {
                subscriber.onNext(List.of(item));
              }

              public void onError(Throwable error) {
                subscriber.onError(error);
              }

              public void onComplete() {
                subscriber.onComplete();
              }
            });
    return json.readTree(subscriber.getBody().toCompletableFuture().get());
  }
}
