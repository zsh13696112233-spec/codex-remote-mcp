package com.codexflow.configcenter.integration.dingtalk;

import com.dingtalk.open.app.api.OpenDingTalkClient;
import com.dingtalk.open.app.api.OpenDingTalkStreamClientBuilder;
import com.dingtalk.open.app.api.callback.DingTalkStreamTopics;
import com.dingtalk.open.app.api.callback.OpenDingTalkCallbackListener;
import com.dingtalk.open.app.api.security.AuthClientCredential;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.function.Consumer;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 基于钉钉官方 Java Stream SDK 和 OpenAPI 的生产通道。 */
@Component
class OfficialDingTalkTransport implements DingTalkTransport {

  private static final String API_HOST = "https://api.dingtalk.com";

  private final DingTalkProperties properties;
  private final ObjectMapper objectMapper;
  private final HttpClient httpClient;
  private volatile OpenDingTalkClient client;
  private volatile String cachedToken;
  private volatile String cachedTokenClientId;
  private volatile long cachedTokenExpiresAt;

  OfficialDingTalkTransport(DingTalkProperties properties, ObjectMapper objectMapper) {
    this.properties = properties;
    this.objectMapper = objectMapper;
    this.httpClient = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
  }

  @Override
  public synchronized void start(
      Consumer<DingTalkModels.Message> messageHandler,
      Consumer<DingTalkModels.CardAction> actionHandler) {
    if (client != null) return;
    OpenDingTalkClient created =
        createClient(
            properties.getClientId(), properties.getClientSecret(), messageHandler, actionHandler);
    try {
      created.start();
      client = created;
    } catch (Exception error) {
      safeStop(created);
      throw new IllegalStateException("钉钉 Stream 长连接启动失败。", error);
    }
  }

  @Override
  public synchronized void stop() {
    OpenDingTalkClient current = client;
    client = null;
    if (current != null) safeStop(current);
  }

  @Override
  public boolean connected() {
    return client != null;
  }

  @Override
  public void testConnection(String clientId, String clientSecret) {
    fetchAccessToken(clientId, clientSecret);
    if (connected()
        && properties.getClientId().equals(clientId)
        && properties.getClientSecret().equals(clientSecret)) return;
    OpenDingTalkClient test = createClient(clientId, clientSecret, ignored -> {}, ignored -> {});
    try {
      test.start();
    } catch (Exception error) {
      throw new IllegalStateException("钉钉 Stream 长连接测试失败。", error);
    } finally {
      safeStop(test);
    }
  }

  @Override
  public DingTalkModels.SendResult sendText(
      String conversationId, String replyToMessageId, String text) {
    return sendGroupMessage(
        conversationId,
        "sampleText",
        objectMapper.createObjectNode().put("content", text),
        "无法生成钉钉文本消息。");
  }

  @Override
  public DingTalkModels.SendResult sendMarkdown(
      String conversationId, String replyToMessageId, String title, String markdown) {
    return sendGroupMessage(
        conversationId,
        "sampleMarkdown",
        objectMapper.createObjectNode().put("title", title).put("text", markdown),
        "无法生成钉钉 Markdown 消息。");
  }

  private DingTalkModels.SendResult sendGroupMessage(
      String conversationId, String msgKey, ObjectNode msgParam, String serializationError) {
    ObjectNode body;
    try {
      body = groupMessageBody(conversationId, msgKey, msgParam);
    } catch (Exception error) {
      throw new IllegalStateException(serializationError, error);
    }
    JsonNode response = authorized("POST", "/v1.0/robot/groupMessages/send", body);
    String messageId = response.path("processQueryKey").asText();
    if (messageId.isBlank()) messageId = UUID.randomUUID().toString();
    return new DingTalkModels.SendResult(messageId);
  }

  ObjectNode groupMessageBody(String conversationId, String msgKey, ObjectNode msgParam)
      throws Exception {
    ObjectNode body = objectMapper.createObjectNode();
    body.put("robotCode", properties.getClientId());
    body.put("openConversationId", conversationId);
    body.put("msgKey", msgKey);
    body.put("msgParam", objectMapper.writeValueAsString(msgParam));
    return body;
  }

  @Override
  public DingTalkModels.SendResult sendCard(
      String conversationId, String replyToMessageId, Map<String, Object> cardData) {
    String cardInstanceId = "codex-" + UUID.randomUUID();
    ObjectNode body = objectMapper.createObjectNode();
    body.put("cardTemplateId", properties.getCardTemplateId());
    body.put("outTrackId", cardInstanceId);
    body.put("callbackType", "STREAM");
    body.put("openSpaceId", "dtv1.card//IM_GROUP." + conversationId);
    body.putObject("imGroupOpenSpaceModel").put("supportForward", true);
    body.putObject("imGroupOpenDeliverModel").put("robotCode", properties.getClientId());
    body.putObject("cardData").set("cardParamMap", stringValues(cardData));
    authorized("POST", "/v1.0/card/instances/createAndDeliver", body);
    return new DingTalkModels.SendResult(cardInstanceId);
  }

  @Override
  public void updateCard(String cardInstanceId, Map<String, Object> cardData) {
    ObjectNode body = objectMapper.createObjectNode();
    body.put("outTrackId", cardInstanceId);
    body.putObject("cardData").set("cardParamMap", stringValues(cardData));
    authorized("PUT", "/v1.0/card/instances", body);
  }

  private OpenDingTalkClient createClient(
      String clientId,
      String clientSecret,
      Consumer<DingTalkModels.Message> messageHandler,
      Consumer<DingTalkModels.CardAction> actionHandler) {
    return OpenDingTalkStreamClientBuilder.custom()
        .credential(new AuthClientCredential(clientId, clientSecret))
        .consumeThreads(4)
        .maxConnectionCounts(1)
        .registerCallbackListener(
            DingTalkStreamTopics.BOT_MESSAGE_TOPIC,
            new OpenDingTalkCallbackListener<String, Void>() {
              @Override
              public Void execute(String request) {
                messageHandler.accept(toMessage(request));
                return null;
              }
            })
        .registerCallbackListener(
            DingTalkStreamTopics.CARD_CALLBACK_TOPIC,
            new OpenDingTalkCallbackListener<String, Map<String, Object>>() {
              @Override
              public Map<String, Object> execute(String request) {
                actionHandler.accept(toAction(request));
                return Map.of();
              }
            })
        .build();
  }

  DingTalkModels.Message toMessage(String request) {
    try {
      JsonNode value = objectMapper.readTree(request);
      String content = value.path("text").path("content").asText();
      if (content.isBlank()) content = value.path("content").path("content").asText();
      String replyTo = firstText(value, "originalMsgId", "replyToMessageId");
      if (replyTo == null) {
        JsonNode replied = value.path("repliedMsg");
        if (replied.isTextual() && !replied.asText().isBlank()) {
          replied = objectMapper.readTree(replied.asText());
        }
        replyTo = firstText(replied, "msgId", "messageId", "originalMsgId");
      }
      return new DingTalkModels.Message(
          value.path("msgId").asText(),
          value.path("conversationId").asText(),
          value.path("conversationType").asText(),
          firstText(value, "senderStaffId", "senderId"),
          content == null ? "" : content.trim(),
          value.path("isInAtList").asBoolean(false),
          mentionAll(value, content),
          replyTo);
    } catch (Exception error) {
      throw new IllegalArgumentException("无法解析钉钉机器人消息。", error);
    }
  }

  @SuppressWarnings("unchecked")
  DingTalkModels.CardAction toAction(String request) {
    try {
      JsonNode value = objectMapper.readTree(request);
      JsonNode content = parseEmbedded(value.path("content"));
      JsonNode privateData = content.path("cardPrivateData");
      JsonNode params = privateData.path("params");
      if (!params.isObject()) params = content.path("params");
      Map<String, Object> actionValues =
          params.isObject() ? objectMapper.convertValue(params, Map.class) : new LinkedHashMap<>();
      String actionId = firstText(privateData, "actionId", "action");
      if (actionId == null) actionId = firstText(content, "actionId", "action");
      if (actionId == null && actionValues.get("action") != null) {
        actionId = actionValues.get("action").toString();
      }
      String openSpaceId = firstText(value, "openSpaceId");
      if (openSpaceId == null) openSpaceId = firstText(content, "openSpaceId");
      String cardInstanceId = firstText(value, "outTrackId", "cardInstanceId");
      if (cardInstanceId == null) {
        cardInstanceId = firstText(content, "outTrackId", "cardInstanceId");
      }
      String operatorUserId = firstText(value, "userId", "staffId", "operatorUserId");
      if (operatorUserId == null) {
        operatorUserId = firstText(content, "userId", "staffId", "operatorUserId");
      }
      return new DingTalkModels.CardAction(
          cardInstanceId, conversationId(openSpaceId), operatorUserId, actionId, actionValues);
    } catch (Exception error) {
      throw new IllegalArgumentException("无法解析钉钉卡片回调。", error);
    }
  }

  private JsonNode parseEmbedded(JsonNode content) throws Exception {
    if (content.isTextual() && !content.asText().isBlank()) {
      return objectMapper.readTree(content.asText());
    }
    return content;
  }

  private synchronized String accessToken() {
    long now = System.currentTimeMillis();
    if (cachedToken != null
        && properties.getClientId().equals(cachedTokenClientId)
        && now + 60_000 < cachedTokenExpiresAt) return cachedToken;
    Token token = fetchAccessToken(properties.getClientId(), properties.getClientSecret());
    cachedToken = token.value();
    cachedTokenClientId = properties.getClientId();
    cachedTokenExpiresAt = now + token.expiresInSeconds() * 1000L;
    return cachedToken;
  }

  private Token fetchAccessToken(String clientId, String clientSecret) {
    ObjectNode body = objectMapper.createObjectNode();
    body.put("appKey", clientId);
    body.put("appSecret", clientSecret);
    JsonNode response = send("POST", "/v1.0/oauth2/accessToken", body, null);
    String value = response.path("accessToken").asText();
    long expiresIn = response.path("expireIn").asLong(7200);
    if (value.isBlank()) throw new IllegalStateException("钉钉未返回访问令牌，请检查应用凭据。");
    return new Token(value, expiresIn);
  }

  private JsonNode authorized(String method, String path, JsonNode body) {
    String token = accessToken();
    try {
      return send(method, path, body, token);
    } catch (UnauthorizedFailure ignored) {
      synchronized (this) {
        cachedToken = null;
        cachedTokenExpiresAt = 0;
      }
      return send(method, path, body, accessToken());
    }
  }

  private JsonNode send(String method, String path, JsonNode body, String token) {
    try {
      HttpRequest.Builder request =
          HttpRequest.newBuilder(URI.create(API_HOST + path))
              .timeout(Duration.ofSeconds(30))
              .header("Content-Type", "application/json")
              .header("Accept", "application/json");
      if (token != null) request.header("x-acs-dingtalk-access-token", token);
      request.method(
          method, HttpRequest.BodyPublishers.ofString(objectMapper.writeValueAsString(body)));
      HttpResponse<String> response =
          httpClient.send(request.build(), HttpResponse.BodyHandlers.ofString());
      if (response.statusCode() == 401) throw new UnauthorizedFailure();
      if (response.statusCode() < 200 || response.statusCode() >= 300) {
        throw new IllegalStateException("钉钉接口调用失败，HTTP " + response.statusCode() + "。");
      }
      return response.body() == null || response.body().isBlank()
          ? objectMapper.createObjectNode()
          : objectMapper.readTree(response.body());
    } catch (UnauthorizedFailure error) {
      throw error;
    } catch (InterruptedException error) {
      Thread.currentThread().interrupt();
      throw new IllegalStateException("钉钉接口调用被中断。", error);
    } catch (Exception error) {
      throw new IllegalStateException("钉钉接口调用失败。", error);
    }
  }

  private ObjectNode stringValues(Map<String, Object> values) {
    ObjectNode result = objectMapper.createObjectNode();
    values.forEach((key, value) -> result.put(key, value == null ? "" : value.toString()));
    return result;
  }

  private static boolean mentionAll(JsonNode value, String content) {
    if (value.path("isAtAll").asBoolean(false)) return true;
    if (content != null && content.contains("@所有人")) return true;
    JsonNode atUsers = value.path("atUsers");
    if (!atUsers.isArray()) return false;
    for (JsonNode user : atUsers) {
      String id = firstText(user, "staffId", "dingtalkId", "userId");
      if ("@ALL".equalsIgnoreCase(id)) return true;
    }
    return false;
  }

  private static String firstText(JsonNode value, String... fields) {
    if (value == null || value.isMissingNode() || value.isNull()) return null;
    for (String field : fields) {
      String text = value.path(field).asText();
      if (text != null && !text.isBlank()) return text;
    }
    return null;
  }

  private static String conversationId(String openSpaceId) {
    if (openSpaceId == null) return null;
    String prefix = "dtv1.card//IM_GROUP.";
    return openSpaceId.startsWith(prefix) ? openSpaceId.substring(prefix.length()) : openSpaceId;
  }

  private static void safeStop(OpenDingTalkClient value) {
    try {
      value.stop();
    } catch (Exception ignored) {
      // 测试连接或应用关闭时继续释放本地引用。
    }
  }

  private record Token(String value, long expiresInSeconds) {}

  private static final class UnauthorizedFailure extends RuntimeException {}
}
