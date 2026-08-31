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
        .contains("任务：测试任务", "状态：🟡 等待确认", "执行进度：█████░░░░░ 50% · 1 / 2", "点击“暂停”", "“立即进入下一步”")
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
        .contains("**任务：** 测试任务", "**状态：** 🟡 执行中", "**执行进度：** █████░░░░░ 50% · 1 / 2", "任务进度已更新。");
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
        .containsEntry("status", "🟡 等待确认")
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
    assertThat(card.get("progressText").toString()).isEqualTo("50% · 1 / 2 步");
    assertThat(card.get("currentStep").toString())
        .contains("### 🎯 当前步骤", "**质量审查**", "质量审查员 · 准备中 · 第 2 次执行");
    assertThat(card.get("stepTimeline").toString())
        .contains("### 📋 步骤状态", "✅ **生成初稿** · 已完成", "• **质量审查** · 准备中");
    assertThat(card.get("latestOutput").toString())
        .contains("> **📄 最近产出 · 生成初稿 · 策略负责人**", "> 初稿已经生成。");
    assertThat(card.get("latestReply").toString()).contains("> **💬 最新助手回复**", "> 质量审查正在等待您确认下一步。");
    assertThat(card.get("notice").toString())
        .contains("### 🟡 等待确认", "下一步：**质量审查 · 质量审查员**", "任务助手已回复。");
    assertThat(card.get("cardBody").toString())
        .contains(
            "**🟡 等待确认 · 1 / 2 步**",
            "**📋 步骤状态**",
            "✅ 生成初稿 · 策略负责人 · 已完成",
            "• 质量审查 · 质量审查员 · 准备中",
            "**下一步：** 质量审查 · 质量审查员",
            "30 秒后自动继续")
        .doesNotContain("最近产出", "最新助手回复", "任务助手已回复");
  }

  @Test
  void completedCardUsesTerminalStyleAndStructuredResult() {
    ObjectNode snapshot = objectMapper.createObjectNode();
    snapshot.put("workflowId", "00000000-0000-4000-8000-000000000101");
    snapshot.put("name", "测试任务");
    snapshot.put("status", "completed");
    snapshot.put("response", "文档已生成并通过质量审查。");
    snapshot.putObject("progress").put("completed", 2).put("total", 2);
    snapshot.putArray("nodes");

    Map<String, Object> card = progress.render(snapshot, "任务已完成。", null);

    assertThat(card)
        .containsEntry("status", "🟢 已完成")
        .containsEntry("flowStatus", "3")
        .containsEntry("progressText", "100% · 2 / 2 步")
        .containsEntry("showConfirm", "false")
        .containsEntry("showHold", "false");
    assertThat(card.get("result").toString()).contains("### 🟢 最终结果", "文档已生成并通过质量审查。");
    assertThat(card.get("cardBody").toString())
        .contains("**🟢 已完成 · 2 / 2 步**", "**✅ 最终结果**", "文档已生成并通过质量审查。")
        .doesNotContain("最新助手回复", "最近产出", "任务已完成。");
    assertThat(card.get("currentStep").toString()).isEmpty();
    assertThat(card.get("latestReply").toString()).isEmpty();
  }

  @Test
  void failedStepUsesFailureEmoji() {
    ObjectNode snapshot = objectMapper.createObjectNode();
    snapshot.put("workflowId", "00000000-0000-4000-8000-000000000101");
    snapshot.put("name", "测试任务");
    snapshot.put("status", "failed");
    snapshot.putObject("progress").put("completed", 0).put("total", 1);
    snapshot
        .putArray("nodes")
        .addObject()
        .put("id", "step-1")
        .put("displayName", "生成文档")
        .put("roleName", "策略负责人")
        .put("status", "failed");

    Map<String, Object> card = progress.render(snapshot, "任务执行失败。", null);

    assertThat(card).containsEntry("status", "🔴 执行失败").containsEntry("flowStatus", "5");
    assertThat(card.get("cardBody").toString())
        .contains("**🔴 执行失败 · 0 / 1 步**", "**📋 步骤状态**", "❌ 生成文档 · 策略负责人 · 失败");
  }
}
