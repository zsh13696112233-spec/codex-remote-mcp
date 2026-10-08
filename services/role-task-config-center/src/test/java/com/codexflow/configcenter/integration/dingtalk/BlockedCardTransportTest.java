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
