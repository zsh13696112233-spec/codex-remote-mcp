package com.codexflow.console;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/** 工作流运行监控中心的 Spring Boot 启动入口。 */
@SpringBootApplication
public class WorkflowConsoleApplication {

  /** 启动监控中心应用及其内嵌 Web 容器。 */
  public static void main(String[] args) {
    SpringApplication.run(WorkflowConsoleApplication.class, args);
  }
}
