package com.codexflow.console;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.core.io.ClassPathResource;
import org.springframework.web.bind.annotation.RequestMethod;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;

/** 验证监控中心 Spring 上下文和受限 API 路由集合。 */
@SpringBootTest(properties = "codex.gateway.base-url=http://127.0.0.1:1")
class WorkflowConsoleApplicationTest {

  @Autowired
  @Qualifier("requestMappingHandlerMapping")
  RequestMappingHandlerMapping mappings;

  /** 确认监控中心只暴露读取、聊天和半自动暂停/继续接口。 */
  @Test
  void applicationContextLoadsWithSevenReadsAndThreeRestrictedPosts() {
    var apiMethods =
        mappings.getHandlerMethods().entrySet().stream()
            .filter(
                entry ->
                    entry.getKey().getPatternValues().stream().anyMatch(p -> p.startsWith("/api/")))
            .toList();
    assertThat(apiMethods).hasSize(9);
    var getRoutes =
        apiMethods.stream()
            .filter(
                entry ->
                    entry.getKey().getMethodsCondition().getMethods().contains(RequestMethod.GET))
            .toList();
    var postRoutes =
        apiMethods.stream()
            .filter(
                entry ->
                    entry.getKey().getMethodsCondition().getMethods().contains(RequestMethod.POST))
            .toList();
    assertThat(getRoutes).hasSize(6);
    assertThat(postRoutes).hasSize(3);
    assertThat(postRoutes)
        .flatExtracting(entry -> entry.getKey().getPatternValues())
        .containsExactlyInAnyOrder(
            "/api/workflows/{workflowId}/messages",
            "/api/workflows/{workflowId}/advance/{gateId}/confirm",
            "/api/workflows/{workflowId}/advance/{gateId}/hold");
    assertThat(apiMethods)
        .noneSatisfy(
            entry ->
                assertThat(entry.getKey().getPatternValues())
                    .anyMatch(
                        path ->
                            path.contains("cancel")
                                || path.contains("retry")
                                || path.contains("skip")
                                || path.contains("edit")));
  }

  /** 确认页面区分进度和讨论消息，并允许终态继续只读咨询。 */
  @Test
  void staticUiKeepsCompletedChatAndDiscussionVisible() throws IOException {
    String app = new ClassPathResource("static/app.js").getContentAsString(StandardCharsets.UTF_8);
    String page =
        new ClassPathResource("static/index.html").getContentAsString(StandardCharsets.UTF_8);

    assertThat(app)
        .contains(
            "任务进度",
            "任务助手",
            "discussionBusy",
            "步执行者",
            "pendingAdvance",
            "确认继续",
            "保持等待",
            "两分钟内未回复将自动继续",
            "明确提出修改或采纳后更新交接总结",
            "讨论处理中",
            "confirmAdvance",
            "holdAdvance",
            "allStepsFinished",
            "所有步骤已完成，正在生成任务总结",
            "所有步骤已完成，但任务总结未完成",
            "allStepsPending",
            "等待任务继续",
            "step-file-link",
            "artifact.mediaType",
            "file.download");
    assertThat(app).doesNotContain("state.snapshot?.status === \"completed\") return");
    assertThat(page).doesNotContain("id=\"retries\"", "剩余重跑次数");
  }
}
