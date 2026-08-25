package com.codexflow.console;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.web.bind.annotation.RequestMethod;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;

/** 验证监控中心 Spring 上下文和受限 API 路由集合。 */
@SpringBootTest(properties = "codex.gateway.base-url=http://127.0.0.1:1")
class WorkflowConsoleApplicationTest {

  @Autowired
  @Qualifier("requestMappingHandlerMapping")
  RequestMappingHandlerMapping mappings;

  /** 确认监控中心只暴露四个读取接口和一个聊天发送接口。 */
  @Test
  void applicationContextLoadsWithFourReadsAndOneChatPostOnly() {
    var apiMethods =
        mappings.getHandlerMethods().entrySet().stream()
            .filter(
                entry ->
                    entry.getKey().getPatternValues().stream().anyMatch(p -> p.startsWith("/api/")))
            .toList();
    assertThat(apiMethods).hasSize(5);
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
    assertThat(postRoutes).hasSize(1);
    assertThat(postRoutes.get(0).getKey().getPatternValues())
        .containsOnly("/api/workflows/{workflowId}/messages");
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
}
