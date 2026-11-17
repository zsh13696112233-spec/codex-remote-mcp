package com.codexflow.configcenter.application;

import com.codexflow.configcenter.integration.dingtalk.JiraEmptyReminderService;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

@Service
public class JiraEmptyReminderScheduler {
  private final JiraEmptyReminderService service;
  private final BoundedWork worker = new BoundedWork("jira-empty-reminder", 1, 1);

  public JiraEmptyReminderScheduler(JiraEmptyReminderService service) {
    this.service = service;
  }

  @Scheduled(fixedDelay = 30_000)
  void tick() {
    worker.submit("remind", service::reconcile);
  }

  @jakarta.annotation.PreDestroy
  void close() {
    worker.close();
  }
}
