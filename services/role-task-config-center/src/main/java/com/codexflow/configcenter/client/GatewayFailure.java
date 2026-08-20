package com.codexflow.configcenter.client;

/** 表示工作流网关返回失败状态或客户端无法完成网关请求。 */
public class GatewayFailure extends RuntimeException {

  private final int statusCode;

  /** 使用网关状态码和响应信息创建异常。 */
  public GatewayFailure(int statusCode, String message) {
    super(message);
    this.statusCode = statusCode;
  }

  /** 使用网关状态码、错误信息和底层原因创建异常。 */
  public GatewayFailure(int statusCode, String message, Throwable cause) {
    super(message, cause);
    this.statusCode = statusCode;
  }

  /** 返回上游状态码；网络类异常统一使用 502。 */
  public int getStatusCode() {
    return statusCode;
  }
}
