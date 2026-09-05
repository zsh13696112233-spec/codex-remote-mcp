package com.codexflow.console.client;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/** 监控中心查询工作流状态、事件和发送对话消息的网关客户端。 */
@Service
public class GatewayClient {

  private static final Pattern FILENAME_PATTERN =
      Pattern.compile("filename=\\\"([^\\\"]*)\\\"", Pattern.CASE_INSENSITIVE);

  private final HttpClient httpClient;
  private final ObjectMapper objectMapper;
  private final URI gatewayBaseUri;

  /** 使用配置的网关地址创建生产环境 HTTP 客户端。 */
  @Autowired
  public GatewayClient(
      ObjectMapper objectMapper,
      @Value("${codex.gateway.base-url:http://127.0.0.1:8080}") String gatewayBaseUrl) {
    this(
        objectMapper,
        gatewayBaseUrl,
        HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build());
  }

  /** 使用显式 HTTP 客户端创建实例，便于测试时替换网络实现。 */
  GatewayClient(ObjectMapper objectMapper, String gatewayBaseUrl, HttpClient httpClient) {
    this.objectMapper = objectMapper;
    this.gatewayBaseUri = URI.create(stripTrailingSlash(gatewayBaseUrl));
    this.httpClient = httpClient;
  }

  /** 查询工作流网关的就绪状态。 */
  public JsonNode ready() {
    return exchange("GET", "/readyz", null);
  }

  /** 查询指定工作流的当前聚合状态。 */
  public JsonNode status(String workflowId) {
    return exchange("GET", "/workflows/" + pathSegment(workflowId), null);
  }

  public JsonNode status(String workflowId, String knownRevision, String knownResults) {
    return exchange(
        "GET",
        "/workflows/"
            + pathSegment(workflowId)
            + "?poll=true"
            + (knownRevision == null ? "" : "&knownRevision=" + pathSegment(knownRevision))
            + (knownResults == null ? "" : "&knownResults=" + pathSegment(knownResults)),
        null);
  }

  public JsonNode events(
      String workflowId, long after, int limit, String view, Long before, boolean tail) {
    return exchange(
        "GET",
        "/workflows/"
            + pathSegment(workflowId)
            + "/events/history?after="
            + after
            + "&limit="
            + limit
            + "&view="
            + pathSegment(view)
            + (before == null ? "" : "&before=" + before)
            + "&tail="
            + tail,
        null);
  }

  /** 按事件游标和数量上限查询指定工作流的历史事件。 */
  public JsonNode events(String workflowId, long after, int limit) {
    return exchange(
        "GET",
        "/workflows/"
            + pathSegment(workflowId)
            + "/events/history?after="
            + after
            + "&limit="
            + limit,
        null);
  }

  /** 读取工作流发布的任意文件附件。 */
  public BinaryResponse artifact(String workflowId, String artifactId) {
    String path = "/workflows/" + pathSegment(workflowId) + "/artifacts/" + pathSegment(artifactId);
    try {
      HttpRequest request =
          HttpRequest.newBuilder(gatewayBaseUri.resolve(path))
              .timeout(Duration.ofSeconds(30))
              .header("Accept", "*/*")
              .GET()
              .build();
      HttpResponse<byte[]> response =
          httpClient.send(request, HttpResponse.BodyHandlers.ofByteArray());
      if (response.statusCode() < 200 || response.statusCode() >= 300) {
        throw new GatewayException(
            response.statusCode(), new String(response.body(), StandardCharsets.UTF_8));
      }
      String contentType =
          response.headers().firstValue("Content-Type").orElse("application/octet-stream");
      String disposition = response.headers().firstValue("Content-Disposition").orElse("");
      return new BinaryResponse(response.body(), contentType, safeFilename(disposition));
    } catch (GatewayException error) {
      throw error;
    } catch (InterruptedException error) {
      Thread.currentThread().interrupt();
      throw new GatewayException(502, "调用 Codex 网关时线程被中断。", error);
    } catch (IOException | IllegalArgumentException error) {
      throw new GatewayException(502, "无法连接 Codex 网关：" + error.getMessage(), error);
    }
  }

  /** 向指定工作流的主监督会话发送消息。 */
  public JsonNode sendMessage(String workflowId, JsonNode body) {
    if (body == null || !body.isObject()) {
      throw new IllegalArgumentException("消息请求必须是 JSON 对象。");
    }
    return exchange("POST", "/workflows/" + pathSegment(workflowId) + "/messages", body);
  }

  /** 确认半自动工作流立即进入下一步骤。 */
  public JsonNode confirmAdvance(String workflowId, String gateId) {
    return exchange(
        "POST",
        "/workflows/" + pathSegment(workflowId) + "/advance/" + pathSegment(gateId) + "/confirm",
        null);
  }

  /** 暂停当前半自动等待，取消倒计时自动流转。 */
  public JsonNode holdAdvance(String workflowId, String gateId) {
    return exchange(
        "POST",
        "/workflows/" + pathSegment(workflowId) + "/advance/" + pathSegment(gateId) + "/hold",
        null);
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
        throw new GatewayException(response.statusCode(), response.body());
      }
      if (response.body() == null || response.body().isBlank()) {
        return objectMapper.createObjectNode();
      }
      return objectMapper.readTree(response.body());
    } catch (GatewayException error) {
      throw error;
    } catch (InterruptedException error) {
      Thread.currentThread().interrupt();
      throw new GatewayException(502, "调用 Codex 网关时线程被中断。", error);
    } catch (IOException | IllegalArgumentException error) {
      throw new GatewayException(502, "无法连接 Codex 网关：" + error.getMessage(), error);
    }
  }

  /** 去除基础地址末尾的斜杠，保证 URI 解析结果一致。 */
  private static String stripTrailingSlash(String value) {
    String result = value.trim();
    while (result.endsWith("/")) {
      result = result.substring(0, result.length() - 1);
    }
    return result;
  }

  /** 校验并进行 URL 编码，将工作流 ID 安全地放入路径段。 */
  private static String pathSegment(String value) {
    if (value == null || value.isBlank()) {
      throw new IllegalArgumentException("workflowId 不能为空。");
    }
    return URLEncoder.encode(value, StandardCharsets.UTF_8).replace("+", "%20");
  }

  private static String safeFilename(String contentDisposition) {
    Matcher matcher =
        FILENAME_PATTERN.matcher(contentDisposition == null ? "" : contentDisposition);
    String filename = matcher.find() ? matcher.group(1) : "artifact.bin";
    filename = filename.replaceAll("[\\r\\n\\\\/\\\"]", "_").trim();
    return filename.isBlank() ? "artifact.bin" : filename;
  }

  /** 网关二进制响应，包含文件正文、类型和安全文件名。 */
  public record BinaryResponse(byte[] body, String contentType, String filename) {}
}
