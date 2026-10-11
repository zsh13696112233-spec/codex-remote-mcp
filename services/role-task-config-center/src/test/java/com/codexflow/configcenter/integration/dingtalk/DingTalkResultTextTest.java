package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

class DingTalkResultTextTest {
  private final ObjectMapper json = new ObjectMapper();

  private String result() {
    var value =
        json.createObjectNode()
            .put("summary", "已核对工单\n请确认方案")
            .put("outcome", "success")
            .put("reason", "");
    value.putNull("document");
    value.putObject("jiraComment").put("status", "not_applicable");
    value.putObject("jiraDeveloper").put("status", "not_applicable");
    return value.toString();
  }

  @Test
  void rendersWholeFencedAndEscapedResultsButPreservesBusinessJson() {
    String raw = result();
    for (String text :
        new String[] {
          raw,
          "```json\n" + raw + "\n```",
          "```json\r\n" + raw + "\r\n```",
          json.writeValueAsString(raw),
          json.writeValueAsString(json.writeValueAsString(raw))
        }) {
      assertThat(DingTalkResultText.display(text)).isEqualTo("已核对工单\n请确认方案");
    }
    for (String text :
        new String[] {
          "普通说明",
          "{\"summary\":\"业务摘要\",\"count\":3}",
          "```json\n{\"price\":198}\n```",
          "示例说明：\n" + raw
        }) {
      assertThat(DingTalkResultText.display(text)).isEqualTo(text);
    }
    var legacy = (ObjectNode) json.readTree(raw);
    legacy.remove("jiraDeveloper");
    assertThat(DingTalkResultText.display(legacy.toString())).isEqualTo("已核对工单\n请确认方案");
  }

  @Test
  void damagedProtocolDoesNotLeakAndBlockedReasonIsRetained() {
    for (String text :
        new String[] {
          "{\"jiraComment\":{},\"summary\":",
          "{\"jiraDeveloper\":{},\"outcome\":\"blocked\"}",
          result() + " trailing"
        }) {
      assertThat(DingTalkResultText.display(text)).isEqualTo("步骤结果暂时无法展示，请查看任务详情。");
    }
    String nested =
        json.writeValueAsString(json.writeValueAsString(json.writeValueAsString(result())));
    assertThat(DingTalkResultText.display(nested)).isEqualTo("步骤结果暂时无法展示，请查看任务详情。");
    var value = (ObjectNode) json.readTree(result());
    value.put("outcome", "blocked").put("reason", "项目查询失败");
    assertThat(DingTalkResultText.display(value.toString())).endsWith("原因：项目查询失败");
    value.put("summary", "项目查询失败");
    assertThat(DingTalkResultText.display(value.toString())).isEqualTo("项目查询失败");
  }

  @Test
  void keepsProtocolCommentaryAsProgressButHidesRawFinalAnswers() {
    for (String source : new String[] {"worker", "supervisor", "assistant"}) {
      var event =
          json.createObjectNode().put("source", source).put("type", "appserver.item/completed");
      var item =
          event.putObject("payload").putObject("message").putObject("params").putObject("item");
      item.put("type", "agentMessage").put("phase", "commentary").put("text", result());
      if (source.equals("assistant")) {
        assertThat(DingTalkExecutionNotice.execution(event)).isEmpty();
      } else {
        assertThat(DingTalkExecutionNotice.execution(event)).isEqualTo("进度说明：\n已核对工单\n请确认方案");
      }
      item.put("phase", "final_answer");
      assertThat(DingTalkExecutionNotice.execution(event)).isEmpty();
      item.remove("phase");
      assertThat(DingTalkExecutionNotice.execution(event)).isEmpty();
      if (!source.equals("assistant")) {
        item.put("phase", "commentary").put("text", "正在核对需求");
        assertThat(DingTalkExecutionNotice.execution(event)).contains("正在核对需求");
      }
    }
  }

  @Test
  void unwrapsProtocolToolEnvelopeAndWaitingCardWithoutChangingGate() {
    var event =
        json.createObjectNode().put("source", "worker").put("type", "appserver.item/completed");
    var item =
        event.putObject("payload").putObject("message").putObject("params").putObject("item");
    item.put("type", "mcpToolCall").put("tool", "business_tool").put("status", "completed");
    var response = item.putObject("result");
    response.putArray("content").addObject().put("type", "text").put("text", result());
    response.set("structuredContent", json.readTree(result()));
    assertThat(DingTalkExecutionNotice.execution(event))
        .contains("已核对工单\n请确认方案")
        .doesNotContain("jiraComment", "structuredContent", "\\n", "not_applicable");
    var snapshot = json.createObjectNode();
    snapshot.putObject("pendingAdvance").put("completedNodeId", "a").put("gateId", "gate");
    snapshot.putArray("nodes").addObject().put("id", "a").put("response", result());
    assertThat(DingTalkWaitingCard.completion(snapshot))
        .contains("已核对工单\n请确认方案", "继续执行")
        .doesNotContain("jiraDeveloper");
    assertThat(snapshot.path("pendingAdvance").path("gateId").asText()).isEqualTo("gate");
    assertThat(snapshot.path("nodes").get(0).path("response").asText()).isEqualTo(result());
  }

  @Test
  void extractedSummaryUsesExistingPublicTextFiltering() {
    var value = (ObjectNode) json.readTree(result());
    value.put("summary", "结果 C:\\private\\file.txt token=secret http://127.0.0.1:8080");
    assertThat(DingTalkResultText.display(value.toString()))
        .doesNotContain("private", "secret", "127.0.0.1");
  }

  @Test
  void intermediateBlockedEnvelopeOnlyShowsProgressSummary() {
    var value = (ObjectNode) json.readTree(result());
    value
        .put("summary", "唯一待规划任务已锁定，开始建立分析基线。")
        .put("outcome", "blocked")
        .put("reason", "方案分析与文件尚未完成。");
    assertThat(DingTalkResultText.progress(value.toString())).isEqualTo("唯一待规划任务已锁定，开始建立分析基线。");
    assertThat(DingTalkResultText.extract(value.toString())).contains("原因：方案分析与文件尚未完成。");
  }

  @Test
  void mixedAndFailedToolResultsKeepTheirDiagnosticFields() {
    var response = json.createObjectNode().put("isError", true).put("error", "工具失败原因");
    response.putArray("content").addObject().put("type", "text").put("text", result());
    assertThat(DingTalkResultText.toolResult(response)).isNull();
    var mixed = response.deepCopy();
    mixed.remove("isError");
    mixed.remove("error");
    mixed.putObject("structuredContent").put("detail", "额外业务数据");
    assertThat(DingTalkResultText.toolResult(mixed)).isNull();
    var event =
        json.createObjectNode().put("source", "worker").put("type", "appserver.item/completed");
    var item =
        event.putObject("payload").putObject("message").putObject("params").putObject("item");
    item.put("type", "mcpToolCall").put("tool", "business_tool").set("result", response);
    assertThat(DingTalkExecutionNotice.execution(event))
        .contains("工具失败原因", "未成功", "已核对工单")
        .doesNotContain("jiraDeveloper", "not_applicable");
    item.set("result", mixed);
    assertThat(DingTalkExecutionNotice.execution(event))
        .contains("额外业务数据", "已核对工单")
        .doesNotContain("jiraDeveloper");
  }
}
