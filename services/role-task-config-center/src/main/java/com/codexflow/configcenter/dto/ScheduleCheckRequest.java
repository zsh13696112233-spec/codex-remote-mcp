package com.codexflow.configcenter.dto;

import jakarta.validation.constraints.*;

public record ScheduleCheckRequest(
    boolean enabled,
    @Size(max = 128) String agentId,
    @Size(max = 1024) String scriptPath,
    @Size(max = 2048) String arguments,
    @Min(5) @Max(120) Integer timeoutSeconds) {
  public ScheduleCheckRequest normalized() {
    if (!enabled) return new ScheduleCheckRequest(false, null, null, null, 60);
    String path = scriptPath == null ? "" : scriptPath.trim();
    String args = arguments == null ? "" : arguments.trim();
    int timeout = timeoutSeconds == null ? 60 : timeoutSeconds;
    if (agentId == null || agentId.isBlank() || agentId.length() > 128)
      throw new IllegalArgumentException("请选择检查执行机。");
    if (path.length() > 1024
        || !path.matches("(?i)^[a-z]:[/\\\\].*\\.(bat|cmd)$")
        || path.substring(2).contains(":")
        || path.matches("(?s).*[\\\"%!?*<>|&^\\r\\n\\x00].*")
        || java.util.Arrays.asList(path.replace('\\', '/').split("/")).contains(".."))
      throw new IllegalArgumentException("请填写 Windows 本地磁盘上的 BAT/CMD 绝对路径，不支持命令符号或相对目录。");
    if (args.length() > 2048
        || args.matches("(?s).*[%!<>|&^\\r\\n\\x00].*")
        || args.chars().filter(c -> c == '"').count() % 2 != 0)
      throw new IllegalArgumentException("参数双引号须成对，不支持变量展开、管道、重定向或命令连接符。");
    if (timeout < 5 || timeout > 120) throw new IllegalArgumentException("检查超时必须为 5 至 120 秒。");
    return new ScheduleCheckRequest(true, agentId, path, args, timeout);
  }
}
