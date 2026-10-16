package com.codexflow.configcenter.application;

import com.codexflow.configcenter.client.GatewayClient;
import com.codexflow.configcenter.client.GatewayFailure;
import com.codexflow.configcenter.domain.TaskScheduleStore;
import org.springframework.stereotype.Service;
import tools.jackson.databind.node.JsonNodeFactory;

/** 检查发生在数据库事务外；每个触发只请求一次，结果不明确时不补发。 */
@Service
public class ScheduleCheckService {
  private final TaskScheduleStore schedules;
  private final GatewayClient gateway;

  public ScheduleCheckService(TaskScheduleStore schedules, GatewayClient gateway) {
    this.schedules = schedules;
    this.gateway = gateway;
  }

  public boolean passes(TaskScheduleStore.Dispatch dispatch) {
    var check = dispatch.check();
    if (!check.enabled()) return true;
    if (!schedules.startCheck(dispatch)) return false;
    var request =
        JsonNodeFactory.instance
            .objectNode()
            .put("agentId", check.agentId())
            .put("groupId", dispatch.groupId())
            .put("scriptPath", check.scriptPath())
            .put("arguments", check.arguments())
            .put("timeoutSeconds", check.timeoutSeconds());
    String status;
    String reason;
    try {
      var response = gateway.checkSchedule(request);
      status = response.path("status").asText();
      reason = response.path("reason").asText();
      if (!java.util.Set.of("passed", "skipped", "failed").contains(status)
          || reason.isBlank()
          || reason.length() > 512) {
        status = "failed";
        reason = "检查服务返回无效结果，本次不再执行。";
      }
    } catch (GatewayFailure error) {
      status = "failed";
      reason = "检查服务不可用或配置校验失败，本次不再执行，请核对执行机及检查配置。";
    }
    return schedules.finishCheck(dispatch, status, reason) && "passed".equals(status);
  }
}
