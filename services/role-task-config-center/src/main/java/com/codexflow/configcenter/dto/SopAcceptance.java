package com.codexflow.configcenter.dto;

/** 原会话串行验收配置，不包含画布数据。 */
public record SopAcceptance(String name, String criteria, Integer maxRepairs) {
  public SopAcceptance {
    if (maxRepairs == null) maxRepairs = 2;
  }

  public void validate() {
    if (name == null
        || name.isBlank()
        || name.length() > 200
        || criteria == null
        || criteria.isBlank()
        || criteria.length() > 10000
        || maxRepairs == null
        || maxRepairs < 0
        || maxRepairs > 10) {
      throw new IllegalArgumentException("判断名称、验收条件必填，自动修复次数必须为 0–10。");
    }
  }
}
