package com.codexflow.configcenter.domain;

/** 表示请求操作与当前领域状态冲突，例如版本过期或对象仍被引用。 */
public class ConflictFailure extends RuntimeException {

  /** 使用可直接返回给调用方的冲突说明创建异常。 */
  public ConflictFailure(String message) {
    super(message);
  }
}
