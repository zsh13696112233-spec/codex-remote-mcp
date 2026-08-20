package com.codexflow.configcenter.domain;

/** 表示请求的角色、SOP、任务定义或运行记录不存在。 */
public class NotFoundFailure extends RuntimeException {

  /** 使用可直接返回给调用方的未找到说明创建异常。 */
  public NotFoundFailure(String message) {
    super(message);
  }
}
