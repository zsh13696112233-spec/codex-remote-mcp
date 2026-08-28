package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.Map;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

class DingTalkProgressCardTest {

  private final ObjectMapper objectMapper = new ObjectMapper();
  private final DingTalkProgressCard progress = new DingTalkProgressCard();

  @Test
  void textProgressUsesPlainLabelsAndExplainsSemiAutomaticCommands() {
    ObjectNode snapshot = objectMapper.createObjectNode();
    snapshot.put("workflowId", "00000000-0000-4000-8000-000000000101");
    snapshot.put("name", "测试任务");
    snapshot.put("status", "running");
    snapshot.putObject("progress").put("completed", 1).put("total", 2);
    snapshot.putArray("nodes");
    snapshot
        .putObject("pendingAdvance")
        .put("gateId", "00000000-0000-4000-8000-000000000201")
        .put("state", "countdown");

    String text = progress.renderText(snapshot, "任务进度已更新。");

    assertThat(text)
        .contains("任务：测试任务", "状态：⏳ 等待确认", "执行进度：█████░░░░░ 50% · 1 / 2", "点击“暂停”", "“立即进入下一步”")
        .doesNotContain("**");
  }

  @Test
  void markdownProgressUsesCardLikeLabelsWithoutCustomTemplateVariables() {
    ObjectNode snapshot = objectMapper.createObjectNode();
    snapshot.put("name", "测试任务");
    snapshot.put("status", "running");
    snapshot.putObject("progress").put("completed", 1).put("total", 2);
    snapshot.putArray("nodes");

    String markdown = progress.renderMarkdown(snapshot, "任务进度已更新。");

    assertThat(markdown)
        .contains("**任务：** 测试任务", "**状态：** 🔵 执行中", "**执行进度：** █████░░░░░ 50% · 1 / 2", "任务进度已更新。");
  }

  @Test
  void cardProgressProvidesTemplateStateAndStreamCallbackVariables() {
    ObjectNode snapshot = objectMapper.createObjectNode();
    snapshot.put("workflowId", "00000000-0000-4000-8000-000000000101");
    snapshot.put("name", "测试任务");
    snapshot.put("status", "running");
    snapshot.putObject("progress").put("completed", 1).put("total", 2);
    snapshot
        .putArray("nodes")
        .addObject()
        .put("id", "step-1")
        .put("displayName", "生成初稿")
        .put("roleName", "策略负责人")
        .put("status", "completed")
        .put("response", "初稿已经生成。");
    snapshot
        .withArray("nodes")
        .addObject()
        .put("id", "step-2")
        .put("displayName", "质量审查")
        .put("roleName", "质量审查员")
        .put("status", "pending")
        .put("attemptCount", 2);
    snapshot
        .putObject("pendingAdvance")
        .put("gateId", "00000000-0000-4000-8000-000000000201")
        .put("nextNodeId", "step-2")
        .put("state", "countdown");

    Map<String, Object> card = progress.render(snapshot, "任务助手已回复。", "质量审查正在等待您确认下一步。");

    assertThat(card)
        .containsEntry("title", "测试任务")
        .containsEntry("status", "⏳ 等待确认")
        .containsEntry("flowStatus", "1")
        .containsEntry("showConfirm", "true")
        .containsEntry("showHold", "true")
        .containsEntry("confirmAction", "advance_confirm")
        .containsEntry("holdAction", "advance_hold")
        .containsEntry("workflowId", "00000000-0000-4000-8000-000000000101")
        .containsEntry("gateId", "00000000-0000-4000-8000-000000000201");
    assertThat(card.get("markdown").toString())
        .contains(
            "**当前步骤：** 质量审查 · 质量审查员",
            "✅ 1. 生成初稿 · 策略负责人 · 已完成",
            "**最近产出 · 生成初稿 · 策略负责人**",
            "初稿已经生成。",
            "**最新助手回复**",
            "质量审查正在等待您确认下一步。",
            "下一步：质量审查 · 质量审查员");
  }
}
