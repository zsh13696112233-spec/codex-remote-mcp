package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.BlockedNotificationStore;
import com.codexflow.configcenter.domain.WorkflowRunStore;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 独立于普通进度消息的阻断通知，只有明确未发送的临时失败才重试。 */
@Service
public class BlockedNotificationService {
  private static final Logger LOG = LoggerFactory.getLogger(BlockedNotificationService.class);
  private final BlockedNotificationStore store;
  private final DingTalkSettingsStore settings;
  private final DingTalkTransport transport;
  private final GatewayClient gateway;
  private final WorkflowRunStore runs;
  private final ObjectMapper json;

  BlockedNotificationService(
      BlockedNotificationStore store,
      DingTalkSettingsStore settings,
      DingTalkTransport transport,
      GatewayClient gateway,
      WorkflowRunStore runs,
      ObjectMapper json) {
    this.store = store;
    this.settings = settings;
    this.transport = transport;
    this.gateway = gateway;
    this.runs = runs;
    this.json = json;
  }

  public List<BlockedNotificationStore.Mapping> mappings() {
    return store.mappings(settings.current().clientId());
  }

  public void mapping(BlockedNotificationStore.Mapping value) {
    store.saveMapping(settings.current().clientId(), value);
  }

  public Map<String, String> config() {
    return Map.of("templateId", store.template());
  }

  public void configure(String templateId) {
    store.saveTemplate(templateId);
  }

  public Map<String, Object> status(String id) {
    return store.view(id);
  }

  public Map<String, Object> test(String requestId, String groupId, String personId, String text) {
    String id = "test-" + java.util.UUID.fromString(requestId);
    var current = settings.current();
    if (!current.enabled()) throw new IllegalArgumentException("请先启用钉钉机器人。");
    String template = store.template();
    if (template.isBlank()) throw new IllegalArgumentException("请先配置并发布专用阻断卡片模板。");
    ObjectNode payload =
        json.createObjectNode()
            .put("_templateId", template)
            .put("title", "阻断通知测试")
            .put("body", safe(text))
            .put("detailLink", "");
    store.reserveTest(id, current.clientId(), groupId, personId, payload);
    return store.view(id);
  }

  public void reconcile() {
    for (String id : store.due("watching")) {
      if (!store.poll(id)) continue;
      try {
        observe(store.read(id));
      } catch (RuntimeException error) {
        LOG.warn("阻断通知暂未完成终态核对，记录={}，异常类型={}。", id, error.getClass().getSimpleName());
      }
    }
  }

  void observe(BlockedNotificationStore.Notice notice) {
    if ("submit_failed".equals(runs.runStatus(notice.workflowId()))) {
      store.finish(notice.id(), "watching", "not_applicable", "任务未成功提交，无需阻断通知。");
      return;
    }
    JsonNode runtime = gateway.get("/workflows/" + notice.workflowId());
    String state = runtime.path("status").asText();
    if (!Set.of("completed", "failed", "cancelled").contains(state)) return;
    JsonNode termination = runtime.path("termination");
    if (!"failed".equals(state) || !"blocked".equals(termination.path("outcome").asText())) {
      store.finish(notice.id(), "watching", "not_applicable", "本次运行无需阻断通知。");
      return;
    }
    JsonNode developer = termination.path("jiraDeveloper");
    if (!"resolved".equals(developer.path("status").asText())) {
      store.finish(
          notice.id(),
          "watching",
          "unavailable",
          switch (developer.path("status").asText()) {
            case "empty" -> "Jira 开发人为空，无法通知。";
            case "multiple" -> "Jira 开发人为多人，无法通知。";
            default -> "未取得唯一 Jira 开发人账号，无法通知。";
          });
      return;
    }
    try {
      String template = store.template();
      if (template.isBlank()) throw new IllegalArgumentException("尚未配置专用阻断卡片模板。");
      var person = store.resolvePerson(notice.group().path("clientId").asText(), developer);
      String step = "阻断步骤";
      int index = 0;
      for (JsonNode node : runtime.path("nodes")) {
        index++;
        if (node.path("id").asText().equals(termination.path("nodeId").asText()))
          step = "第" + index + "步：" + node.path("displayName").asText();
      }
      var comment = termination.path("jiraComment");
      String commentLabel =
          switch (comment.path("status").asText()) {
            case "succeeded" -> "已备注";
            case "failed" -> "备注失败";
            case "unknown" -> "备注结果未确认";
            default -> "未备注";
          };
      ObjectNode payload =
          json.createObjectNode()
              .put("_templateId", template)
              .put("title", "Jira 任务因阻断结束")
              .put(
                  "body",
                  safe(
                      "Jira："
                          + comment.path("issueKey").asText()
                          + "\n开发人："
                          + developer.path("displayName").asText()
                          + "\n"
                          + step
                          + "\n原因："
                          + termination.path("reason").asText()
                          + "\n处理角色与 Jira 备注："
                          + commentLabel
                          + "。"
                          + comment.path("detail").asText()))
              .put("detailLink", "[查看运行详情](" + runs.monitorUrl(notice.workflowId()) + ")");
      store.prepare(notice.id(), person, payload);
    } catch (IllegalArgumentException error) {
      store.finish(notice.id(), "watching", "unavailable", safe(error.getMessage()));
    }
  }

  public void deliver() {
    for (String id : store.due("pending")) {
      try {
        send(id);
      } catch (RuntimeException error) {
        LOG.warn("阻断通知状态保存暂未完成，记录={}，异常类型={}。", id, error.getClass().getSimpleName());
      }
    }
  }

  void send(String id) {
    var notice = store.claim(id);
    if (notice == null) return;
    var current = settings.current();
    if (!current.enabled()
        || !store.available(notice.group(), "GROUP", current.clientId())
        || !store.available(notice.person(), "PERSON", current.clientId())) {
      store.finish(id, "sending", "unavailable", "原通知群、人员或机器人已停用或不可用。");
      return;
    }
    Map<String, Object> payload = new LinkedHashMap<>();
    notice
        .payload()
        .properties()
        .forEach(entry -> payload.put(entry.getKey(), entry.getValue().asText()));
    try {
      transport.sendBlockedCard(
          "blocked-" + id,
          notice.group().path("clientId").asText(),
          notice.group().path("externalId").asText(),
          notice.person().path("externalId").asText(),
          payload);
    } catch (NotSentFailure error) {
      if (error.retryable) store.retry(notice);
      else store.finish(id, "sending", "failed", "钉钉拒绝发送，请检查模板、应用权限和群成员配置。");
      return;
    } catch (RuntimeException error) {
      store.finish(id, "sending", "unknown", "发送结果未确认，未自动重发；请核查群内卡片。");
      return;
    }
    // 外部成功与本地提交之间失败时，保留 sending，超时后转 unknown，绝不再次发送。
    store.finish(id, "sending", "delivered", "已投递，是否已读及客户端提醒需在钉钉核验。");
  }

  static String safe(String value) {
    String text =
        DingTalkExecutionNotice.safe(value == null ? "" : value)
            .replaceAll("\\b(?:\\d{1,3}\\.){3}\\d{1,3}(?::\\d+)?\\b", "[地址已隐藏]")
            .replaceAll(
                "(?i)(thread|turn|agent|会话)(?:Id|编号)?\\s*[:=：]\\s*[^\\s,，;；]+", "[内部编号已隐藏]");
    return text.length() <= 6000 ? text : text.substring(0, 6000) + "\n[内容已截断]";
  }

  static class NotSentFailure extends RuntimeException {
    final boolean retryable;

    NotSentFailure(boolean retryable) {
      super("卡片未发送。");
      this.retryable = retryable;
    }
  }
}
