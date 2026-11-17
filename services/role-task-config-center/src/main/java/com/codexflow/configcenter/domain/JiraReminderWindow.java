package com.codexflow.configcenter.domain;

import java.time.*;
import java.util.*;
import tools.jackson.databind.JsonNode;

/** 北京时间周时间表；相邻及重叠时段属于同一个连续窗口。 */
public final class JiraReminderWindow {
  private static final ZoneId ZONE = ZoneId.of("Asia/Shanghai");

  private JiraReminderWindow() {}

  public record Window(Instant start, Instant end) {}

  public static void validate(JsonNode config) {
    JsonNode days = config.path("days"), windows = config.path("windows");
    if (!days.isArray()
        || days.isEmpty()
        || days.size() > 7
        || !windows.isArray()
        || windows.isEmpty()
        || windows.size() > 24) throw new IllegalArgumentException("请选择生效星期和时间段（最多24段）。");
    Set<Integer> seen = new HashSet<>();
    for (JsonNode day : days)
      if (!day.isIntegralNumber() || day.asInt() < 1 || day.asInt() > 7 || !seen.add(day.asInt()))
        throw new IllegalArgumentException("生效星期无效或重复。");
    for (JsonNode window : windows) {
      LocalTime start = time(window.path("start").asText()),
          end = time(window.path("end").asText());
      if (start.equals(end)) throw new IllegalArgumentException("开始与结束时间不能相同。");
    }
  }

  private static LocalTime time(String value) {
    if (!value.matches("(?:[01][0-9]|2[0-3]):[0-5][0-9]"))
      throw new IllegalArgumentException("时间格式应为 HH:mm。");
    return LocalTime.parse(value);
  }

  public static Window current(JsonNode config, Instant now) {
    List<Window> ranges = new ArrayList<>();
    LocalDate today = now.atZone(ZONE).toLocalDate();
    for (int offset = -8; offset <= 8; offset++) {
      LocalDate date = today.plusDays(offset);
      boolean enabled = false;
      for (JsonNode day : config.path("days"))
        enabled |= day.asInt() == date.getDayOfWeek().getValue();
      if (!enabled) continue;
      for (JsonNode item : config.path("windows")) {
        LocalTime start = time(item.path("start").asText()), end = time(item.path("end").asText());
        ranges.add(
            new Window(
                date.atTime(start).atZone(ZONE).toInstant(),
                date.plusDays(end.isBefore(start) ? 1 : 0).atTime(end).atZone(ZONE).toInstant()));
      }
    }
    ranges.sort(Comparator.comparing(Window::start));
    List<Window> merged = new ArrayList<>();
    for (Window range : ranges) {
      if (!merged.isEmpty() && !range.start().isAfter(merged.get(merged.size() - 1).end())) {
        Window old = merged.remove(merged.size() - 1);
        merged.add(
            new Window(old.start(), old.end().isAfter(range.end()) ? old.end() : range.end()));
      } else merged.add(range);
    }
    for (Window range : merged)
      if (!now.isBefore(range.start()) && now.isBefore(range.end())) {
        // 覆盖整周时没有自然窗口边界，使用稳定的无限窗口，避免每天重置。
        if (Duration.between(range.start(), range.end()).toDays() >= 7)
          return new Window(
              Instant.parse("1970-01-01T00:00:00Z"), Instant.parse("9999-01-01T00:00:00Z"));
        return range;
      }
    return null;
  }
}
