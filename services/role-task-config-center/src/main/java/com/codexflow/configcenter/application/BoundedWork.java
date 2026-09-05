package com.codexflow.configcenter.application;

import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.Semaphore;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/** 有界后台执行器：同一业务键不重入，调度线程不等待网络调用。 */
public final class BoundedWork implements AutoCloseable {
  private static final Logger LOGGER = LoggerFactory.getLogger(BoundedWork.class);
  private final Set<String> active = ConcurrentHashMap.newKeySet();
  private final Semaphore capacity;
  private final ThreadPoolExecutor executor;

  public BoundedWork(String name, int workers, int queueSize) {
    capacity = new Semaphore(workers + queueSize);
    executor =
        new ThreadPoolExecutor(
            workers,
            workers,
            30,
            TimeUnit.SECONDS,
            new LinkedBlockingQueue<>(workers + queueSize),
            task -> {
              Thread thread = new Thread(task, name);
              thread.setDaemon(true);
              return thread;
            });
    executor.allowCoreThreadTimeOut(true);
  }

  public int available() {
    return capacity.availablePermits();
  }

  public boolean submit(String key, Runnable task) {
    if (!active.add(key)) return false;
    if (!capacity.tryAcquire()) {
      active.remove(key);
      return false;
    }
    try {
      executor.execute(
          () -> {
            try {
              task.run();
            } catch (RuntimeException error) {
              LOGGER.warn("后台任务处理失败，下一轮将继续检查。", error);
            } finally {
              active.remove(key);
              capacity.release();
            }
          });
      return true;
    } catch (java.util.concurrent.RejectedExecutionException error) {
      active.remove(key);
      capacity.release();
      return false;
    }
  }

  @Override
  public void close() {
    executor.shutdownNow();
  }
}
