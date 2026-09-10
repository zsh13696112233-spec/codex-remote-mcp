package com.codexflow.configcenter.integration.dingtalk;

import static org.assertj.core.api.Assertions.*;

import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.springframework.dao.CannotAcquireLockException;

class DingTalkDatabaseRetryTest {
  @Test
  void retriesLockFailureAndReturnsRecoveredResult() {
    var calls = new AtomicInteger();
    String result =
        DingTalkDatabaseRetry.execute(
            "测试",
            () -> {
              if (calls.incrementAndGet() < 3) throw new CannotAcquireLockException("test");
              return "ok";
            });
    assertThat(result).isEqualTo("ok");
    assertThat(calls.get()).isEqualTo(3);
  }

  @Test
  void stopsAfterThreeAttemptsAndDoesNotRetryBusinessFailures() {
    var calls = new AtomicInteger();
    assertThatThrownBy(
            () ->
                DingTalkDatabaseRetry.run(
                    "测试",
                    () -> {
                      calls.incrementAndGet();
                      throw new CannotAcquireLockException("test");
                    }))
        .isInstanceOf(CannotAcquireLockException.class);
    assertThat(calls.get()).isEqualTo(3);
    calls.set(0);
    assertThatThrownBy(
            () ->
                DingTalkDatabaseRetry.run(
                    "测试",
                    () -> {
                      calls.incrementAndGet();
                      throw new IllegalArgumentException("业务错误");
                    }))
        .isInstanceOf(IllegalArgumentException.class);
    assertThat(calls.get()).isEqualTo(1);
  }
}
