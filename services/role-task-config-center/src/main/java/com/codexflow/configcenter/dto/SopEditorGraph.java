package com.codexflow.configcenter.dto;

import java.util.List;

/** 与执行协议分离的 SOP 编辑图；版本 2 在串行链中增加验收关卡。 */
public record SopEditorGraph(Integer version, List<Node> nodes, List<Edge> edges) {
  public record Node(String id, String type, Double x, Double y, SopAcceptance acceptance) {
    public Node(String id, String type, Double x, Double y) {
      this(id, type, x, y, null);
    }
  }

  public record Edge(String source, String target) {}
}
