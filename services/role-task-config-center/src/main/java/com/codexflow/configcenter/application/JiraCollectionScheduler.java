package com.codexflow.configcenter.application;

import com.codexflow.configcenter.domain.JiraCollectionService;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

/** 采集不受 SOP 定时规则和执行机空闲状态影响。 */
@Service
public class JiraCollectionScheduler {
  private final JiraCollectionService collections;
  private final BoundedWork worker = new BoundedWork("jira-collection", 1, 1);

  public JiraCollectionScheduler(JiraCollectionService collections) {
    this.collections = collections;
  }

  @Scheduled(fixedDelay = 30_000)
  void tick() {
    worker.submit("collect", collections::synchronize);
  }

  @jakarta.annotation.PreDestroy
  void close() {
    worker.close();
  }
}
