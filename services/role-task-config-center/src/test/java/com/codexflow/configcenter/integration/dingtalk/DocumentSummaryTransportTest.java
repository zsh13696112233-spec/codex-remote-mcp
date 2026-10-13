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

class DocumentSummaryTransportTest {
  private final HttpClient http = mock(HttpClient.class);

  private OfficialDingTalkTransport transport() {
    var properties = new DingTalkProperties();
    properties.setClientId("test-client");
    properties.setClientSecret("test-placeholder");
    return new OfficialDingTalkTransport(properties, new ObjectMapper(), http);
  }

  @SuppressWarnings("unchecked")
  private HttpResponse<String> response(int code, String body) {
    HttpResponse<String> response = mock(HttpResponse.class);
    when(response.statusCode()).thenReturn(code);
    when(response.body()).thenReturn(body);
    return response;
  }

  private DingTalkModels.SendResult send(OfficialDingTalkTransport transport) {
    return transport.sendDocumentSummaryCard(
        "documents-stable",
        "PERSON",
        "person",
        Map.of("_templateId", "wide.schema", "summary", "完成"));
  }

  @Test
  void uncertainSendNeverRetriesAndExactRecipientReceiptIsRequired() throws Exception {
    for (String body :
        new String[] {
          "{}",
          "{\"success\":true,\"result\":{\"deliverResults\":[{\"spaceType\":\"IM_GROUP\",\"spaceId\":\"person\",\"success\":true}]}}"
        }) {
      reset(http);
      when(http.send(
              any(HttpRequest.class),
              org.mockito.ArgumentMatchers.<HttpResponse.BodyHandler<String>>any()))
          .thenAnswer(call -> response(200, "{\"accessToken\":\"test-token\"}"))
          .thenAnswer(call -> response(200, body));
      assertThatThrownBy(() -> send(transport())).isInstanceOf(IllegalStateException.class);
      verify(http, times(2)).send(any(HttpRequest.class), any());
    }
    reset(http);
    when(http.send(
            any(HttpRequest.class),
            org.mockito.ArgumentMatchers.<HttpResponse.BodyHandler<String>>any()))
        .thenAnswer(call -> response(200, "{\"accessToken\":\"test-token\"}"))
        .thenThrow(new IOException("lost response"));
    assertThatThrownBy(() -> send(transport())).isInstanceOf(IllegalStateException.class);
    verify(http, times(2)).send(any(HttpRequest.class), any());
  }

  @Test
  void temporaryRejectionAndPermanentRejectionAreDistinguished() throws Exception {
    for (int code : new int[] {401, 429, 400}) {
      reset(http);
      when(http.send(
              any(HttpRequest.class),
              org.mockito.ArgumentMatchers.<HttpResponse.BodyHandler<String>>any()))
          .thenAnswer(call -> response(200, "{\"accessToken\":\"test-token\"}"))
          .thenAnswer(call -> response(code, "{}"));
      assertThatThrownBy(() -> send(transport()))
          .isInstanceOfSatisfying(
              DingTalkDocumentSummaryCard.NotSentFailure.class,
              error -> assertThat(error.retryable).isEqualTo(code != 400));
      verify(http, times(2)).send(any(HttpRequest.class), any());
    }
  }

  @Test
  void singleChatReceiptReturnsCarrierAndMissingTemplateDoesNotSend() throws Exception {
    var transport = transport();
    assertThatThrownBy(
            () -> transport.sendDocumentSummaryCard("card", "PERSON", "person", Map.of()))
        .isInstanceOf(DingTalkDocumentSummaryCard.NotSentFailure.class);
    verifyNoInteractions(http);
    when(http.send(
            any(HttpRequest.class),
            org.mockito.ArgumentMatchers.<HttpResponse.BodyHandler<String>>any()))
        .thenAnswer(call -> response(200, "{\"accessToken\":\"test-token\"}"))
        .thenAnswer(
            call ->
                response(
                    200,
                    "{\"success\":true,\"result\":{\"deliverResults\":[{\"spaceType\":\"IM_ROBOT\",\"spaceId\":\"person\",\"success\":true,\"carrierId\":\"receipt\"}]}}"));
    assertThat(send(transport).messageId()).isEqualTo("receipt");
  }
}
