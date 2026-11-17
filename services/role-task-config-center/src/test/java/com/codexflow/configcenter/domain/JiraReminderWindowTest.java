package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.*;

import java.time.*;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;

class JiraReminderWindowTest {
  final ObjectMapper json = new ObjectMapper();

  @Test
  void midnightBelongsToStartDayAndEndIsExclusive() {
    var c = json.readTree("{\"days\":[4],\"windows\":[{\"start\":\"22:00\",\"end\":\"02:00\"}]}");
    JiraReminderWindow.validate(c);
    assertThat(JiraReminderWindow.current(c, Instant.parse("2026-11-12T17:00:00Z"))).isNotNull();
    assertThat(JiraReminderWindow.current(c, Instant.parse("2026-11-12T18:00:00Z"))).isNull();
    assertThat(JiraReminderWindow.current(c, Instant.parse("2026-11-13T17:00:00Z"))).isNull();
  }

  @Test
  void adjacentWindowsMergeAndInvalidTimesRejected() {
    var c =
        json.readTree(
            "{\"days\":[4],\"windows\":[{\"start\":\"09:00\",\"end\":\"12:00\"},{\"start\":\"12:00\",\"end\":\"18:00\"}]}");
    assertThat(JiraReminderWindow.current(c, Instant.parse("2026-11-12T04:00:00Z")).start())
        .isEqualTo(Instant.parse("2026-11-12T01:00:00Z"));
    assertThatThrownBy(
            () ->
                JiraReminderWindow.validate(
                    json.readTree(
                        "{\"days\":[1],\"windows\":[{\"start\":\"25:00\",\"end\":\"18:00\"}]}")))
        .isInstanceOf(IllegalArgumentException.class);
  }
}
