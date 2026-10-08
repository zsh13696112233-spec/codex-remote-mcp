package com.codexflow.configcenter.application;

import com.codexflow.configcenter.integration.dingtalk.BlockedNotificationService;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
class BlockedNotificationScheduler {
  private final BlockedNotificationService service;
  private final BoundedWork polling = new BoundedWork("blocked-notification-poll", 1, 1);
  private final BoundedWork delivery = new BoundedWork("blocked-notification-delivery", 1, 1);

  BlockedNotificationScheduler(BlockedNotificationService service) {
    this.service = service;
  }

  @Scheduled(fixedDelay = 5000)
  void tick() {
    polling.submit("poll", service::reconcile);
    delivery.submit("send", service::deliver);
  }

  @jakarta.annotation.PreDestroy
  void close() {
    polling.close();
    delivery.close();
  }
}
