package com.codexflow.configcenter;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

/** 角色、SOP 和任务配置中心的 Spring Boot 启动入口。 */
@SpringBootApplication
@EnableScheduling
public class ConfigCenterApplication {

  /** 启动配置中心应用及其内嵌 Web 容器。 */
  public static void main(String[] args) {
    SpringApplication.run(ConfigCenterApplication.class, args);
  }
}
