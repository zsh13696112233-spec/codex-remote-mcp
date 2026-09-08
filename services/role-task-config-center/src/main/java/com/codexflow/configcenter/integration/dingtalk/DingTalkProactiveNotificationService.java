package com.codexflow.configcenter.integration.dingtalk;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.domain.ConflictFailure;
import com.codexflow.configcenter.domain.DingTalkTaskBindingDirectory;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/** 为网页和定时运行建立钉钉绑定，并发送第一条主动进度消息。 */
@Service
public class DingTalkProactiveNotificationService {

  private static final Logger LOGGER =
      LoggerFactory.getLogger(DingTalkProactiveNotificationService.class);

  private final DingTalkSettingsStore settings;
  private final DingTalkTaskBindingDirectory taskBindings;
  private final DingTalkStore store;
  private final GatewayClient gateway;
  private final DingTalkProgressCard progressCard;
  private final ObjectMapper objectMapper;

  DingTalkProactiveNotificationService(
      DingTalkSettingsStore settings,
      DingTalkTaskBindingDirectory taskBindings,
      DingTalkStore store,
      GatewayClient gateway,
      DingTalkProgressCard progressCard,
      ObjectMapper objectMapper) {
    this.settings = settings;
    this.taskBindings = taskBindings;
    this.store = store;
    this.gateway = gateway;
    this.progressCard = progressCard;
    this.objectMapper = objectMapper;
  }

  /** 在创建运行记录前校验机器人和任务通知对象，避免产生无效运行。 */
  public void validate(String taskId) {
    DingTalkSettingsStore.Settings current = enabledSettings();
    taskBindings.validateProactive(taskId, current.clientId());
  }

  /** 创建冻结目标的主动通知绑定。 */
  public void reserve(String taskId, String workflowId, String triggerSource) {
    DingTalkSettingsStore.Settings current = enabledSettings();
    store.reserveProactive(current.clientId(), taskId, workflowId, triggerSource);
  }

  /** 网关接受工作流后激活绑定并尽力生成第一条进度消息。 */
  public void submitted(String workflowId, String notice) {
    try {
      store.markSubmitted(workflowId);
      JsonNode snapshot = gateway.get("/workflows/" + workflowId);
      DingTalkModels.Binding binding = store.binding(workflowId).orElseThrow();
      store.enqueueText(
          "proactive-start:" + workflowId,
          workflowId,
          binding.conversationId(),
          binding.rootMessageId(),
          "工作流编号："
              + workflowId
              + "\n"
              + DingTalkExecutionNotice.safe(snapshot.path("name").asText() + "：" + notice));
    } catch (RuntimeException error) {
      LOGGER.warn("钉钉主动任务已提交，但首条进度消息暂未生成，workflowId={}。", workflowId, error);
    }
  }

  /** 提交失败时释放通知槽并通过 Outbox 发送稳定错误消息。 */
  public void submissionFailed(String workflowId) {
    try {
      DingTalkSettingsStore.Settings current = settings.current();
      store.markSubmissionFailed(current.clientId(), workflowId, "任务启动失败，请稍后在网页重新运行。");
    } catch (RuntimeException error) {
      LOGGER.warn("记录钉钉主动任务提交失败状态时发生异常，workflowId={}。", workflowId, error);
    }
  }

  private DingTalkSettingsStore.Settings enabledSettings() {
    DingTalkSettingsStore.Settings current = settings.current();
    if (!current.enabled() || current.clientId().isBlank()) {
      throw new ConflictFailure("钉钉机器人尚未配置并启用，不能推送任务消息。");
    }
    return current;
  }
}
