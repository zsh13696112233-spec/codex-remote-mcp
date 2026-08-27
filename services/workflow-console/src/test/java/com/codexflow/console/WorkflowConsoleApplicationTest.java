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
  void applicationContextLoadsWithFourReadsAndThreeRestrictedPosts() {
    var apiMethods =
        mappings.getHandlerMethods().entrySet().stream()
            .filter(
                entry ->
                    entry.getKey().getPatternValues().stream().anyMatch(p -> p.startsWith("/api/")))
            .toList();
    assertThat(apiMethods).hasSize(7);
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
    assertThat(getRoutes).hasSize(4);
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

  /** 确认页面区分进度和助手消息、展示额度，并允许终态继续发送消息。 */
  @Test
  void staticUiKeepsCompletedChatAndRetryPolicyVisible() throws IOException {
    String app = new ClassPathResource("static/app.js").getContentAsString(StandardCharsets.UTF_8);
    String page =
        new ClassPathResource("static/index.html").getContentAsString(StandardCharsets.UTF_8);

    assertThat(app)
        .contains(
            "任务进度",
            "任务助手",
            "snapshot.retryPolicy",
            "remainingRetries",
            "pendingAdvance",
            "立即进入下一步",
            "暂停，暂不进入下一步",
            "继续进入下一步",
            "暂停不会返工",
            "请在任务助手中说明修改点",
            "confirmAdvance",
            "holdAdvance",
            "step-file-link",
            "artifact.mediaType",
            "file.download");
    assertThat(app).doesNotContain("state.snapshot?.status === \"completed\") return");
    assertThat(page).contains("id=\"retries\"", "剩余重跑次数");
  }
}
