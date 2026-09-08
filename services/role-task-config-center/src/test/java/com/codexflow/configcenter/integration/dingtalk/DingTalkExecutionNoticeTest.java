package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

class DingTalkExecutionNoticeTest {
  private final ObjectMapper json = new ObjectMapper();

  private ObjectNode event(String method, String type) {
    var event = json.createObjectNode().put("type", "appserver." + method).put("source", "worker");
    event
        .putObject("payload")
        .putObject("message")
        .putObject("params")
        .putObject("item")
        .put("type", type);
    return event;
  }

  private ObjectNode item(ObjectNode event) {
    return (ObjectNode) event.path("payload").path("message").path("params").path("item");
  }

  @Test
  void hidesOnlySupervisorPollingToolsAcrossTheirLifecycle() {
    for (String type : new String[] {"mcpToolCall", "dynamicToolCall"}) {
      for (String method : new String[] {"item/started", "item/completed"}) {
        for (String tool : new String[] {"wait_node", "node_status", "workflow_status"}) {
          var event = event(method, type).put("source", "supervisor");
          item(event).put("tool", tool);
          assertThat(DingTalkExecutionNotice.execution(event)).isEmpty();
          for (String source : new String[] {"worker", "assistant"}) {
            event.put("source", source);
            assertThat(DingTalkExecutionNotice.execution(event)).contains("工具调用：");
          }
        }
      }
    }
  }

  @Test
  void retainsSupervisorProgressAndActionsAndNamedWorkerFileChanges() {
    var event = event("item/completed", "agentMessage").put("source", "supervisor");
    item(event).put("phase", "commentary").put("text", "策划已完成，开始开发");
    assertThat(DingTalkExecutionNotice.execution(event)).contains("策划已完成，开始开发");
    for (String tool : new String[] {"dispatch_node", "cancel_node"}) {
      event = event("item/started", "mcpToolCall").put("source", "supervisor");
      item(event).put("tool", tool);
      assertThat(DingTalkExecutionNotice.execution(event)).startsWith("正在");
    }
    event = event("item/completed", "fileChange").put("nodeId", "a");
    var snapshot = json.createObjectNode();
    snapshot.putArray("nodes").addObject().put("id", "a").put("displayName", "开发");
    assertThat(
            DingTalkExecutionNotice.stepLabel(event, snapshot)
                + "\n"
                + DingTalkExecutionNotice.execution(event))
        .isEqualTo("步骤「开发」\n工具调用：修改文件 · 已完成");
  }

  @Test
  void supervisorActionsUseStepNamesWithoutJsonAndKeepWorkerDetails() {
    var snapshot = json.createObjectNode();
    snapshot.putArray("nodes").addObject().put("id", "node-a").put("displayName", "策划");
    var event = event("item/started", "mcpToolCall").put("source", "supervisor");
    item(event).put("tool", "dispatch_node");
    item(event).putObject("arguments").put("node_id", "node-a").put("workflow_id", "workflow-a");
    item(event).putObject("result").put("internal", "raw-result");
    assertThat(DingTalkExecutionNotice.execution(event, snapshot)).isEqualTo("正在启动步骤「策划」");
    event.put("type", "appserver.item/completed");
    assertThat(DingTalkExecutionNotice.execution(event, snapshot)).isEqualTo("已启动步骤「策划」");
    item(event).put("tool", "cancel_node");
    assertThat(DingTalkExecutionNotice.execution(event, snapshot)).isEqualTo("已提交步骤「策划」的停止请求");
    item(event).put("status", "failed");
    assertThat(DingTalkExecutionNotice.execution(event, snapshot))
        .isEqualTo("停止步骤「策划」未成功，请查看任务进度。");
    item(event).put("status", "completed");
    ((ObjectNode) item(event).path("result")).put("isError", true);
    assertThat(DingTalkExecutionNotice.execution(event, snapshot)).contains("未成功");
    event.put("source", "worker");
    assertThat(DingTalkExecutionNotice.execution(event, snapshot))
        .contains("node-a", "workflow-a", "raw-result");
  }

  @Test
  void stepLabelUsesWorkflowNodeContract() {
    var event = event("item/started", "webSearch").put("nodeId", "a");
    var snapshot = json.createObjectNode();
    snapshot.putArray("nodes").addObject().put("id", "a").put("displayName", "资料核对");
    assertThat(DingTalkExecutionNotice.stepLabel(event, snapshot)).isEqualTo("步骤「资料核对」");
  }

  @Test
  void reportsOriginalCommandAndCompletedOutput() {
    var event = event("item/started", "commandExecution");
    item(event).put("command", "secret-command").put("aggregatedOutput", "secret-output");
    item(event).put("cwd", "C:\\work\\project");
    assertThat(DingTalkExecutionNotice.execution(event))
        .contains("工具调用：执行命令 · 开始执行", "secret-command", "C:\\work\\project")
        .doesNotContain("secret-output");
    event.put("type", "appserver.item/completed");
    item(event).put("exitCode", 1);
    assertThat(DingTalkExecutionNotice.execution(event))
        .contains("工具调用：执行命令 · 未成功", "secret-output", "退出码：1");
  }

  @Test
  void showsFileOperationsAndCompletedDiff() {
    var event = event("item/started", "fileChange");
    var change = item(event).putArray("changes").addObject();
    change
        .put("path", "C:\\work\\main.js")
        .put("diff", "--- a/main.js\n+++ b/main.js\n-old\n+new\n+next");
    change.putObject("kind").put("type", "update");
    assertThat(DingTalkExecutionNotice.execution(event))
        .contains("C:\\work\\main.js", "操作：修改")
        .doesNotContain("修改片段");
    event.put("type", "appserver.item/completed");
    assertThat(DingTalkExecutionNotice.execution(event))
        .contains("新增 2 行，删除 1 行", "-old\n+new\n+next");
  }

  @Test
  void showsToolArgumentsAndResultsWithoutMaskingButExcludesImageTransport() {
    var event = event("item/completed", "mcpToolCall");
    item(event).put("tool", "example_tool").put("durationMs", 32);
    item(event)
        .putObject("arguments")
        .put("token", "example-secret")
        .put("url", "http://internal.example/api")
        .put("downloadCode", "image-code");
    var content = item(event).putObject("result").putArray("content");
    content.addObject().put("type", "text").put("text", "完成");
    content.addObject().put("type", "image").put("data", "binary-image");
    assertThat(DingTalkExecutionNotice.execution(event))
        .contains("example-secret", "http://internal.example/api", "完成", "耗时（毫秒）：32")
        .doesNotContain("image-code", "binary-image");
  }

  @Test
  void boundsDetailsAndMarksPreviouslyTruncatedEvents() {
    var event = event("item/completed", "commandExecution");
    item(event).put("aggregatedOutput", "x".repeat(5000));
    ((ObjectNode) event.path("payload")).put("truncated", true);
    assertThat(DingTalkExecutionNotice.execution(event))
        .contains("[内容已截断]", "[原始事件详情已截断]")
        .hasSizeLessThan(4100);
  }

  @Test
  void onlyPublishesCompletedReadableSummary() {
    var event = event("item/started", "reasoning");
    item(event).putArray("summary").add("先检查输入").addObject().put("text", "再核对结果");
    item(event).putArray("content").add("private raw reasoning");
    assertThat(DingTalkExecutionNotice.execution(event)).isEmpty();
    event.put("type", "appserver.item/completed");
    assertThat(DingTalkExecutionNotice.execution(event)).isEqualTo("思考摘要：\n先检查输入\n\n再核对结果");
    item(event).remove("summary");
    assertThat(DingTalkExecutionNotice.execution(event)).isEmpty();
  }

  @Test
  void ignoresRawDeltasAndAssistantStructuredAnswer() {
    var event = event("item/reasoning/textDelta", "reasoning");
    assertThat(DingTalkExecutionNotice.execution(event)).isEmpty();
    event = event("item/completed", "agentMessage");
    item(event).put("phase", "commentary").put("text", "{\"kind\":\"stop\"}");
    event.put("source", "assistant");
    assertThat(DingTalkExecutionNotice.execution(event)).isEmpty();
  }

  @Test
  void preservesPublicDeliverablesWhileRemovingInternalAndCredentialUrls() {
    String result =
        "报告：[下载](https://files.example.com/reports/report.pdf?version=2#download)\n"
            + "来源 https://www.example.org/docs/start\n"
            + "内部 http://127.0.0.1:8080/workflows/id http://10.0.0.2/data https://gateway.local/a\n"
            + "密钥 https://api.example.com/data?access_token=secret https://user:pass@example.org/data";
    assertThat(DingTalkExecutionNotice.safe(result))
        .contains(
            "[下载](https://files.example.com/reports/report.pdf?version=2#download)",
            "https://www.example.org/docs/start")
        .doesNotContain("127.0.0.1", "10.0.0.2", "gateway.local", "access_token", "user:pass");
  }

  @Test
  void removesSensitiveLocationsAndCredentialsFromPublicText() {
    String text =
        "正在读取 C:\\private\\input.png https://internal.example/x token=secret sk-abc123 thread_123";
    assertThat(DingTalkExecutionNotice.safe(text))
        .doesNotContain("private", "internal.example", "secret", "sk-abc123", "thread_123");
  }
}
