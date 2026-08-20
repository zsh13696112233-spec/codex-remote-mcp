package com.codexflow.console.client;

/** 表示工作流网关返回失败状态或监控中心无法完成网关请求。 */
public class GatewayException extends RuntimeException {

  private final int statusCode;
  private final String responseBody;

  /** 使用上游状态码和原始响应体创建异常。 */
  public GatewayException(int statusCode, String responseBody) {
    super("Codex 网关返回 HTTP " + statusCode);
    this.statusCode = statusCode;
    this.responseBody = responseBody;
  }

  /** 使用统一状态码、错误信息和底层原因创建客户端异常。 */
  public GatewayException(int statusCode, String message, Throwable cause) {
    super(message, cause);
    this.statusCode = statusCode;
    this.responseBody = message;
  }

  /** 返回上游 HTTP 状态码；网络类异常统一使用 502。 */
  public int getStatusCode() {
    return statusCode;
  }

  /** 返回上游响应体或本地生成的错误说明。 */
  public String getResponseBody() {
    return responseBody;
  }
}
