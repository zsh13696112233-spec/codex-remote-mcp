package com.codexflow.configcenter.domain;

import com.codexflow.configcenter.dto.SopEditorGraph;
import com.codexflow.configcenter.dto.SopStepRequest;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** 图是执行顺序的唯一来源，坐标和请求数组次序不参与排序。 */
final class SopGraphValidator {
  private SopGraphValidator() {}

  static List<SopStepRequest> ordered(SopEditorGraph graph, List<SopStepRequest> steps) {
    if (steps == null || steps.isEmpty() || steps.stream().anyMatch(java.util.Objects::isNull))
      fail();
    // 兼容已有 API 调用方；没有图时沿用原串行顺序。
    if (graph == null) {
      Set<String> keys = new HashSet<>();
      for (var step : steps) {
        if (step.nodeKey() != null && (!validId(step.nodeKey()) || !keys.add(step.nodeKey())))
          fail();
      }
      return steps;
    }
    if ((!Integer.valueOf(1).equals(graph.version()) && !Integer.valueOf(2).equals(graph.version()))
        || graph.nodes() == null
        || graph.edges() == null
        || steps.isEmpty()
        || graph.nodes().size()
            != steps.size()
                + 2
                + graph.nodes().stream()
                    .filter(n -> n != null && "acceptance".equals(n.type()))
                    .count()
        || graph.edges().size() != graph.nodes().size() - 1) fail();
    Map<String, SopEditorGraph.Node> nodes = new HashMap<>();
    String start = null;
    String end = null;
    for (var node : graph.nodes()) {
      if (node == null
          || !validId(node.id())
          || nodes.put(node.id(), node) != null
          || node.x() == null
          || node.y() == null
          || !Double.isFinite(node.x())
          || !Double.isFinite(node.y())
          || Math.abs(node.x()) > 1000000
          || Math.abs(node.y()) > 1000000) fail();
      if ("start".equals(node.type())) {
        if (start != null) fail();
        start = node.id();
      } else if ("end".equals(node.type())) {
        if (end != null) fail();
        end = node.id();
      } else if ("acceptance".equals(node.type())) {
        if (!Integer.valueOf(2).equals(graph.version()) || node.acceptance() == null) fail();
        node.acceptance().validate();
      } else if (!"step".equals(node.type())) fail();
      if (!"acceptance".equals(node.type()) && node.acceptance() != null) fail();
    }
    if (start == null || end == null) fail();
    Map<String, SopStepRequest> byKey = new HashMap<>();
    for (var step : steps) {
      if (!validId(step.nodeKey())
          || byKey.put(step.nodeKey(), step) != null
          || !nodes.containsKey(step.nodeKey())
          || !"step".equals(nodes.get(step.nodeKey()).type())) fail();
    }
    Map<String, String> next = new HashMap<>();
    Set<String> incoming = new HashSet<>();
    for (var edge : graph.edges()) {
      if (edge == null
          || !nodes.containsKey(edge.source())
          || !nodes.containsKey(edge.target())
          || edge.source().equals(edge.target())
          || edge.source().equals(end)
          || edge.target().equals(start)
          || next.put(edge.source(), edge.target()) != null
          || !incoming.add(edge.target())) fail();
    }
    List<SopStepRequest> ordered = new ArrayList<>();
    Set<String> visited = new HashSet<>();
    String current = start;
    String previous = null;
    while (current != null && visited.add(current)) {
      if ("acceptance".equals(nodes.get(current).type()) && !byKey.containsKey(previous)) fail();
      if (byKey.containsKey(current)) ordered.add(byKey.get(current));
      if (current.equals(end)) break;
      previous = current;
      current = next.get(current);
    }
    if (!end.equals(current) || visited.size() != nodes.size() || ordered.size() != steps.size())
      fail();
    return ordered;
  }

  private static boolean validId(String value) {
    return value != null && value.matches("[A-Za-z0-9_-]{1,128}");
  }

  private static void fail() {
    throw new IllegalArgumentException("画布必须由开始、全部角色步骤和结束组成一条完整串行链，且节点与步骤配置一一对应。");
  }
}
