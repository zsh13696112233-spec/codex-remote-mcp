package com.codexflow.configcenter.client;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/** 配置中心调用内部工作流网关的同步 HTTP 客户端。 */
@Component
public class GatewayClient {

  private final ObjectMapper objectMapper;
  private final HttpClient httpClient;
  private final URI gatewayBaseUri;

  /** 注入 JSON 映射器、网关地址并创建带连接超时的 HTTP 客户端。 */
  GatewayClient(
      ObjectMapper objectMapper,
      @Value("${codex.gateway.base-url:http://127.0.0.1:8080}") String gatewayBaseUrl) {
    this.objectMapper = objectMapper;
    this.gatewayBaseUri = URI.create(stripTrailingSlash(gatewayBaseUrl));
    this.httpClient = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
  }

  /** 向网关指定路径发送 GET 请求。 */
  public JsonNode get(String path) {
    return exchange("GET", path, null);
  }

  /** 向网关指定路径发送 POST 请求，可传入空请求体。 */
  public JsonNode post(String path, JsonNode body) {
    return exchange("POST", path, body);
  }

  /** 构建并执行 HTTP 请求，将成功响应解析为 JSON，并统一转换网络异常。 */
  private JsonNode exchange(String method, String path, JsonNode body) {
    try {
      HttpRequest.Builder request =
          HttpRequest.newBuilder(gatewayBaseUri.resolve(path))
              .timeout(Duration.ofSeconds(30))
              .header("Accept", "application/json");
      if (body == null) {
        request.method(method, HttpRequest.BodyPublishers.noBody());
      } else {
        request
            .header("Content-Type", "application/json; charset=utf-8")
            .method(
                method,
                HttpRequest.BodyPublishers.ofString(
                    objectMapper.writeValueAsString(body), StandardCharsets.UTF_8));
      }

      HttpResponse<String> response =
          httpClient.send(
              request.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
      if (response.statusCode() < 200 || response.statusCode() >= 300) {
        throw new GatewayFailure(response.statusCode(), response.body());
      }
      if (response.body() == null || response.body().isBlank()) {
        return objectMapper.createObjectNode();
      }
      return objectMapper.readTree(response.body());
    } catch (GatewayFailure error) {
      throw error;
    } catch (InterruptedException error) {
      Thread.currentThread().interrupt();
      throw new GatewayFailure(502, "调用工作流网关时线程被中断。", error);
    } catch (IOException | IllegalArgumentException error) {
      throw new GatewayFailure(502, "无法连接工作流网关：" + error.getMessage(), error);
    }
  }

  /** 去除基础地址末尾的斜杠，保证后续路径拼接一致。 */
  private static String stripTrailingSlash(String value) {
    String result = value.trim();
    while (result.endsWith("/")) {
      result = result.substring(0, result.length() - 1);
    }
    return result;
  }
}
