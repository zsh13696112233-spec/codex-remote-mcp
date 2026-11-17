package com.codexflow.configcenter.application;

/** Jira 页码查询；未指定页码时沿用历史游标协议。 */
public record JiraPagination(Integer page, int pageSize) {
  public JiraPagination {
    if (page != null && page < 1 || pageSize < 1 || pageSize > 100)
      throw new IllegalArgumentException("分页参数无效。");
  }

  public String query(Long before) {
    if (page != null && before != null) throw new IllegalArgumentException("分页参数无效。");
    return page == null ? "" : "&page=" + page + "&pageSize=" + pageSize;
  }
}
