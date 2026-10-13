package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;

class DingTalkDocumentSummaryCardTest {
  private final ObjectMapper json = new ObjectMapper();

  @Test
  void allDocumentsAreVisibleWithoutControlButtonsOrInvalidLinks() {
    var snapshot =
        json.createObjectNode()
            .put("advanceMode", "automatic")
            .put("name", "任务")
            .put("status", "completed");
    var docs = snapshot.putArray("documents");
    for (int i = 0; i < 7; i++)
      docs.addObject()
          .put("id", "文档 " + i)
          .put("name", "文档" + i + ".md")
          .put("available", true)
          .put("stepNumber", i + 1)
          .put("stepName", "交付");
    docs.addObject()
        .put("id", "missing")
        .put("name", "未同步.txt")
        .put("error", "读取失败")
        .put("available", false);
    docs.addObject()
        .put("id", "previous")
        .put("name", "旧正文.md")
        .put("error", "读取失败")
        .put("available", true);
    var data =
        DingTalkDocumentSummaryCard.render(
            "wf", snapshot, "最终结果", "https://monitor.example/", "template");
    String text = data.get("documents").toString();
    for (int i = 0; i < 7; i++) assertThat(text).contains("文档" + i + ".md");
    assertThat(text)
        .contains("%E6%96%87%E6%A1%A3+6", "查看上次成功版本", "最新版本未同步")
        .doesNotContain("documentId=missing");
    assertThat(data)
        .containsEntry("showConfirm", "false")
        .containsEntry("showStop", "false")
        .containsEntry("summary", "最终结果")
        .containsEntry("_templateId", "template");
    assertThat(data).doesNotContainKeys("gateId", "confirmAction");
  }

  @Test
  void allTerminalStatesAndEmptyDocumentsRespectEligibility() {
    var snapshot = json.createObjectNode().put("advanceMode", "automatic");
    snapshot.putArray("documents");
    assertThat(DingTalkDocumentSummaryCard.applicable(snapshot)).isFalse();
    snapshot.withArray("documents").addObject().put("name", "结果.txt");
    for (String status : List.of("completed", "failed", "cancelled")) {
      snapshot.put("status", status);
      assertThat(DingTalkDocumentSummaryCard.applicable(snapshot)).isTrue();
      assertThat(
              DingTalkDocumentSummaryCard.render(
                      "wf", snapshot, "结果", "https://monitor.example", "template")
                  .get("statusText"))
          .isEqualTo(
              switch (status) {
                case "completed" -> "已完成";
                case "failed" -> "执行失败";
                default -> "已取消";
              });
    }
    snapshot.put("advanceMode", "semi_automatic");
    assertThat(DingTalkDocumentSummaryCard.applicable(snapshot)).isFalse();
  }
}
