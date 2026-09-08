package com.codexflow.configcenter.integration.dingtalk;

import java.net.URI;
import java.util.ArrayList;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import tools.jackson.databind.JsonNode;

/** 展示公开摘要和有长度上限的工具详情，不转发原始推理或图片传输内容。 */
final class DingTalkExecutionNotice {
  private DingTalkExecutionNotice() {}

  static String execution(JsonNode event) {
    return execution(event, null);
  }

  static String execution(JsonNode event, JsonNode snapshot) {
    boolean started = "appserver.item/started".equals(event.path("type").asText());
    boolean completed = "appserver.item/completed".equals(event.path("type").asText());
    if (!started && !completed) return "";
    JsonNode item = event.path("payload").path("message").path("params").path("item");
    String type = item.path("type").asText();
    // 只隐藏主监督的轮询工具；业务步骤及任务助手的同名工具仍正常展示。
    if ("supervisor".equals(event.path("source").asText())
        && ("mcpToolCall".equals(type) || "dynamicToolCall".equals(type))
        && switch (item.path("tool").asText()) {
          case "wait_node", "node_status", "workflow_status" -> true;
          default -> false;
        }) return "";
    String text = "";
    if (completed && "reasoning".equals(type)) {
      StringBuilder summary = new StringBuilder();
      for (JsonNode part : item.path("summary")) {
        String value = part.isTextual() ? part.asText() : part.path("text").asText();
        if (!value.isBlank()) {
          if (!summary.isEmpty()) summary.append("\n\n");
          summary.append(value);
        }
      }
      if (!summary.isEmpty()) text = "思考摘要：\n" + summary;
    } else if (completed
        && "agentMessage".equals(type)
        && "commentary".equals(item.path("phase").asText())
        && !"assistant".equals(event.path("source").asText())) {
      text = "进度说明：\n" + item.path("text").asText();
    } else {
      String tool =
          switch (type) {
            case "commandExecution" -> "执行命令";
            case "fileChange" -> "修改文件";
            case "webSearch" -> "搜索资料";
            case "imageView" -> "查看图片";
            case "imageGeneration" -> "生成图片";
            case "mcpToolCall", "dynamicToolCall" -> toolName(item.path("tool").asText());
            case "collabToolCall", "collabAgentToolCall" -> "协作任务";
            default -> "";
          };
      if (!tool.isBlank()) {
        String status = item.path("status").asText();
        boolean failed =
            "failed".equals(status)
                || "declined".equals(status)
                || "cancelled".equals(status)
                || (!item.path("error").isMissingNode() && !item.path("error").isNull())
                || (item.has("success") && !item.path("success").asBoolean())
                || item.path("result").path("isError").asBoolean()
                || (item.hasNonNull("exitCode") && item.path("exitCode").asInt() != 0);
        if ("supervisor".equals(event.path("source").asText())
            && ("mcpToolCall".equals(type) || "dynamicToolCall".equals(type))) {
          String action = item.path("tool").asText();
          if ("dispatch_node".equals(action) || "cancel_node".equals(action)) {
            String target = "步骤";
            String nodeId = item.path("arguments").path("node_id").asText();
            if (snapshot != null && !nodeId.isBlank()) {
              for (JsonNode node : snapshot.path("nodes")) {
                if (nodeId.equals(node.path("id").asText())) {
                  target = "步骤「" + node.path("displayName").asText("未命名") + "」";
                  break;
                }
              }
            }
            String verb = "dispatch_node".equals(action) ? "启动" : "停止";
            if (started) return "正在" + verb + target;
            if (failed) return verb + target + "未成功，请查看任务进度。";
            return "dispatch_node".equals(action) ? "已启动" + target : "已提交" + target + "的停止请求";
          }
        }
        text = "工具调用：" + tool + " · " + (started ? "开始执行" : failed ? "未成功" : "已完成");
        return toolDetails(
            text, type, item, completed, event.path("payload").path("truncated").asBoolean());
      }
    }
    return safe(text);
  }

  private static String toolDetails(
      String heading, String type, JsonNode item, boolean completed, boolean truncated) {
    var text = new StringBuilder(heading);
    switch (type) {
      case "commandExecution" -> {
        detail(text, "命令", item.path("command"));
        detail(text, "工作目录", item.path("cwd"));
        if (completed) {
          detail(text, "退出码", item.path("exitCode"));
          detail(text, "输出", item.path("aggregatedOutput"));
        }
      }
      case "fileChange" -> {
        int count = 0;
        for (JsonNode change : item.path("changes")) {
          if (++count > 10) {
            text.append("\n[文件列表已截断]");
            break;
          }
          detail(text, "文件", change.path("path"));
          JsonNode kind = change.path("kind");
          String operation = kind.isTextual() ? kind.asText() : kind.path("type").asText();
          text.append("\n操作：")
              .append(
                  switch (operation) {
                    case "add" -> "新增";
                    case "delete" -> "删除";
                    default -> "修改";
                  });
          detail(text, "移动到", kind.path("move_path"));
          if (completed && change.path("diff").isTextual()) {
            String diff = change.path("diff").asText();
            long added =
                diff.lines()
                    .filter(line -> line.startsWith("+") && !line.startsWith("+++"))
                    .count();
            long removed =
                diff.lines()
                    .filter(line -> line.startsWith("-") && !line.startsWith("---"))
                    .count();
            text.append("\n变更：新增 ").append(added).append(" 行，删除 ").append(removed).append(" 行");
            detail(text, "修改片段", change.path("diff"));
          }
        }
      }
      case "mcpToolCall", "dynamicToolCall" -> {
        detail(text, "参数", item.path("arguments"));
        if (completed) {
          detail(text, "结果", item.path("result"));
          detail(text, "结果内容", item.path("contentItems"));
        }
      }
      case "webSearch" -> {
        detail(text, "查询", item.path("query"));
        detail(text, "操作", item.path("action"));
      }
      case "imageView" -> detail(text, "文件", item.path("path"));
      case "imageGeneration" -> detail(text, "输出文件", item.path("savedPath"));
      default -> {
        detail(text, "任务要求", item.path("prompt"));
      }
    }
    if (completed) {
      detail(text, "耗时（毫秒）", item.path("durationMs"));
      detail(text, "错误", item.path("error"));
    }
    if (truncated) text.append("\n[原始事件详情已截断]");
    return clipped(text.toString(), 4000);
  }

  private static void detail(StringBuilder text, String label, JsonNode value) {
    if (value.isMissingNode() || value.isNull()) return;
    JsonNode display = withoutImageTransport(value);
    String content = display.isTextual() ? display.asText() : display.toString();
    if (!content.isBlank())
      text.append("\n").append(label).append("：").append(clipped(content, 1500));
  }

  // 仅排除图片和钉钉传输字段；工具命令、路径、参数及文本结果按原内容展示。
  private static JsonNode withoutImageTransport(JsonNode value) {
    if (value.isObject()) {
      var copy = ((tools.jackson.databind.node.ObjectNode) value).deepCopy();
      String type = value.path("type").asText();
      if ("image".equals(type) || "image_url".equals(type) || "input_image".equals(type)) {
        copy.removeAll();
        return copy.put("说明", "图片内容不在工具消息中展示");
      }
      for (var entry : value.properties()) {
        String key = entry.getKey();
        if (key.matches(
            "(?i)(download_?code|session_?webhook|sessionWebhookExpiredTime|download_?url|temporary_?url|b64_json|base64|image_?data|image_?url|result_?data)")) {
          copy.remove(key);
        } else copy.set(key, withoutImageTransport(entry.getValue()));
      }
      return copy;
    }
    if (value.isArray()) {
      var copy = ((tools.jackson.databind.node.ArrayNode) value).deepCopy();
      for (int i = 0; i < copy.size(); i++) copy.set(i, withoutImageTransport(value.get(i)));
      return copy;
    }
    return value;
  }

  private static String clipped(String text, int limit) {
    if (text.length() <= limit) return text;
    int end = limit;
    if (Character.isHighSurrogate(text.charAt(end - 1))) end--;
    return text.substring(0, end) + "\n[内容已截断]";
  }

  private static String toolName(String tool) {
    String known =
        Map.of(
                "dispatch_node",
                "启动步骤",
                "wait_node",
                "等待步骤",
                "node_status",
                "查询步骤进度",
                "workflow_status",
                "查询任务进度",
                "cancel_node",
                "停止步骤")
            .get(tool);
    if (known != null) return known;
    return tool.matches("[A-Za-z][A-Za-z0-9_-]{0,63}") ? tool : "调用工具";
  }

  static String stepLabel(JsonNode event, JsonNode snapshot) {
    String id = event.path("nodeId").asText("");
    if (id.isBlank()) id = event.path("payload").path("nodeId").asText("");
    for (JsonNode node : snapshot.path("nodes")) {
      if (!id.isBlank() && id.equals(node.path("id").asText())) {
        return safe("步骤「" + node.path("displayName").asText("未命名") + "」");
      }
    }
    return "assistant".equals(event.path("source").asText()) ? "任务助手" : "任务执行";
  }

  static String safe(String text) {
    var links = new ArrayList<String>();
    var matcher = Pattern.compile("(?i)(?:https?|wss?)://[^\\s<>\"，。；）)\\]]+").matcher(text);
    StringBuffer protectedText = new StringBuffer();
    while (matcher.find()) {
      String url = matcher.group();
      String replacement = "[链接已隐藏]";
      if (publicLink(url)) {
        replacement = "\uE000" + links.size() + "\uE001";
        links.add(url);
      }
      matcher.appendReplacement(protectedText, Matcher.quoteReplacement(replacement));
    }
    matcher.appendTail(protectedText);
    String result =
        protectedText
            .toString()
            .replaceAll("[A-Za-z]:[\\\\/][^\\s<>\"，。；]+", "[路径已隐藏]")
            .replaceAll("(?<![\\w])/(?:[^\\s/]+/)+[^\\s，。；]*", "[路径已隐藏]")
            .replaceAll("(?i)(?:bearer\\s+\\S+|sk-[A-Za-z0-9_-]+)", "[凭据已隐藏]")
            .replaceAll("(?i)(token|password|secret|api[_-]?key)\\s*[:=]\\s*[^\\s,;]+", "$1=[已隐藏]")
            .replaceAll("\\b(?:thr|thread|turn|agent)_[A-Za-z0-9_-]+", "[内部编号已隐藏]");
    for (int index = 0; index < links.size(); index++)
      result = result.replace("\uE000" + index + "\uE001", links.get(index));
    return result;
  }

  private static boolean publicLink(String url) {
    try {
      URI uri = URI.create(url);
      String host = uri.getHost();
      if (host == null
          || uri.getUserInfo() != null
          || !("https".equalsIgnoreCase(uri.getScheme())
              || "http".equalsIgnoreCase(uri.getScheme()))) return false;
      host = host.toLowerCase(Locale.ROOT).replaceAll("\\.$", "");
      // 不解析 DNS：内部主机名、裸 IP 和带凭据的 URL 不进入用户消息。
      if (!host.contains(".")
          || host.contains(":")
          || host.matches("[0-9.]+")
          || host.matches(".*\\.(local|localhost|lan|internal|intranet|test|example)")
          || host.matches("(internal|intranet|private)(\\..*)?")) return false;
      String details =
          (uri.getRawQuery() == null ? "" : uri.getRawQuery())
              + "&"
              + (uri.getRawFragment() == null ? "" : uri.getRawFragment());
      details = java.net.URLDecoder.decode(details, java.nio.charset.StandardCharsets.UTF_8);
      return !Pattern.compile(
              "(?i)(?:^|[&;])[^=&;]*(?:token|secret|password|signature|credential|api[_-]?key|access[_-]?key|authorization)[^=&;]*=")
          .matcher(details)
          .find();
    } catch (IllegalArgumentException error) {
      return false;
    }
  }
}
