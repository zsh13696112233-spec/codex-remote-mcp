package com.codexflow.configcenter.integration.dingtalk;

import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Supplier;
import org.slf4j.LoggerFactory;
import org.springframework.dao.CannotAcquireLockException;

/** 仅包裹独立的数据库事务调用；不可包含远端发送或网关控制操作。 */
final class DingTalkDatabaseRetry {
  private DingTalkDatabaseRetry() {}

  static <T> T execute(String operation, Supplier<T> transaction) {
    for (int attempt = 1; ; attempt++) {
      try {
        T result = transaction.get();
        if (attempt > 1)
          LoggerFactory.getLogger(DingTalkDatabaseRetry.class)
              .info("钉钉数据库操作已恢复：操作={}，尝试次数={}。", operation, attempt);
        return result;
      } catch (CannotAcquireLockException error) {
        if (attempt >= 3) throw error;
        LoggerFactory.getLogger(DingTalkDatabaseRetry.class)
            .warn("钉钉数据库锁竞争，准备重试：操作={}，尝试次数={}。", operation, attempt);
        try {
          Thread.sleep(ThreadLocalRandom.current().nextLong(20, 61) * attempt);
        } catch (InterruptedException interrupted) {
          Thread.currentThread().interrupt();
          throw error;
        }
      }
    }
  }

  static void run(String operation, Runnable transaction) {
    execute(
        operation,
        () -> {
          transaction.run();
          return null;
        });
  }
}
