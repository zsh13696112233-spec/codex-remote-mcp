package com.codexflow.configcenter.dto;

import java.util.List;

/** 与执行协议分离的 SOP 编辑图；版本 1 仅允许一条串行链。 */
public record SopEditorGraph(Integer version, List<Node> nodes, List<Edge> edges) {
  public record Node(String id, String type, Double x, Double y) {}

  public record Edge(String source, String target) {}
}
