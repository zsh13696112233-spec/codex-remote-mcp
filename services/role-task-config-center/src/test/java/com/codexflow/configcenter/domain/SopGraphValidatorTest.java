package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;

import com.codexflow.configcenter.dto.SopEditorGraph;
import com.codexflow.configcenter.dto.SopStepRequest;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.Test;

class SopGraphValidatorTest {
  private SopStepRequest step(String key) {
    return new SopStepRequest(
        key,
        "same-role",
        "执行",
        "结果",
        "local",
        "machine",
        null,
        false,
        "read_only",
        null,
        60,
        Set.of(),
        Set.of(),
        key);
  }

  private SopEditorGraph graph(List<SopEditorGraph.Edge> edges) {
    return new SopEditorGraph(
        1,
        List.of(
            new SopEditorGraph.Node("start", "start", 0d, 0d),
            new SopEditorGraph.Node("a", "step", 800d, 100d),
            new SopEditorGraph.Node("b", "step", -200d, 0d),
            new SopEditorGraph.Node("end", "end", 400d, 0d)),
        edges);
  }

  private SopEditorGraph.Edge edge(String a, String b) {
    return new SopEditorGraph.Edge(a, b);
  }

  @Test
  void ordersByEdgesNotCoordinatesOrRequestOrder() {
    assertThat(
            SopGraphValidator.ordered(
                graph(List.of(edge("start", "a"), edge("a", "b"), edge("b", "end"))),
                List.of(step("b"), step("a"))))
        .extracting(SopStepRequest::nodeKey)
        .containsExactly("a", "b");
  }

  @Test
  void rejectsDisconnectedCyclesForksMergesAndSelfLoops() {
    for (var edges :
        List.of(
            List.of(edge("start", "end"), edge("a", "b"), edge("b", "a")),
            List.of(edge("start", "a"), edge("start", "b"), edge("b", "end")),
            List.of(edge("start", "b"), edge("a", "b"), edge("b", "end")),
            List.of(edge("start", "a"), edge("a", "a"), edge("b", "end")),
            List.of(edge("start", "a")))) {
      assertThatThrownBy(
              () -> SopGraphValidator.ordered(graph(edges), List.of(step("a"), step("b"))))
          .isInstanceOf(IllegalArgumentException.class);
    }
  }

  @Test
  void rejectsInvalidMetadataAndStepMapping() {
    var valid = graph(List.of(edge("start", "a"), edge("a", "b"), edge("b", "end")));
    assertThatThrownBy(() -> SopGraphValidator.ordered(valid, List.of(step("a"), step("a"))))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(() -> SopGraphValidator.ordered(valid, List.of(step("a"), step("missing"))))
        .isInstanceOf(IllegalArgumentException.class);
    var nodes = new ArrayList<>(valid.nodes());
    nodes.set(0, new SopEditorGraph.Node("start", "start", Double.NaN, 0d));
    assertThatThrownBy(
            () ->
                SopGraphValidator.ordered(
                    new SopEditorGraph(1, nodes, valid.edges()), List.of(step("a"), step("b"))))
        .isInstanceOf(IllegalArgumentException.class);
    assertThatThrownBy(
            () ->
                SopGraphValidator.ordered(
                    new SopEditorGraph(3, valid.nodes(), valid.edges()),
                    List.of(step("a"), step("b"))))
        .isInstanceOf(IllegalArgumentException.class);
  }

  @Test
  void legacyRequestKeepsSerialOrderButRejectsDuplicateKeys() {
    var steps = List.of(step("b"), step("a"));
    assertThat(SopGraphValidator.ordered(null, steps)).isEqualTo(steps);
    assertThatThrownBy(() -> SopGraphValidator.ordered(null, List.of(step("a"), step("a"))))
        .isInstanceOf(IllegalArgumentException.class);
  }

  @Test
  void acceptanceGateFollowsItsRoleAndDoesNotBecomeAnExecutionStep() {
    var nodes =
        List.of(
            new SopEditorGraph.Node("start", "start", 0d, 0d),
            new SopEditorGraph.Node("a", "step", 10d, 0d),
            new SopEditorGraph.Node(
                "check",
                "acceptance",
                20d,
                0d,
                new com.codexflow.configcenter.dto.SopAcceptance("检查", "满足预期", 2)),
            new SopEditorGraph.Node("end", "end", 30d, 0d));
    var graph =
        new SopEditorGraph(
            2, nodes, List.of(edge("start", "a"), edge("a", "check"), edge("check", "end")));
    assertThat(SopGraphValidator.ordered(graph, List.of(step("a")))).hasSize(1);
    var invalid =
        new SopEditorGraph(
            2, nodes, List.of(edge("start", "check"), edge("check", "a"), edge("a", "end")));
    assertThatThrownBy(() -> SopGraphValidator.ordered(invalid, List.of(step("a"))))
        .isInstanceOf(IllegalArgumentException.class);
  }
}
