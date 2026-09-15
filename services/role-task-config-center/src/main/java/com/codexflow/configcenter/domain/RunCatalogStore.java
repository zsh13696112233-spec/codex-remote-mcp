package com.codexflow.configcenter.domain;

import jakarta.persistence.EntityManager;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.node.ObjectNode;

/** 跨任务运行目录，只投影列表字段，沿用实体时间映射，不加载快照与结果。 */
@Service
public class RunCatalogStore {
  private final EntityManager entities;
  private final DomainJsonMapper json;

  public RunCatalogStore(EntityManager entities, DomainJsonMapper json) {
    this.entities = entities;
    this.json = json;
  }

  @Transactional(readOnly = true)
  public ObjectNode list(
      String name, String type, String startDate, String endDate, int page, int size) {
    return list(name, type, startDate, endDate, page, size, "");
  }

  @Transactional(readOnly = true)
  public ObjectNode list(
      String name,
      String type,
      String startDate,
      String endDate,
      int page,
      int size,
      String groupId) {
    if (page < 0 || page > 100000 || size < 1 || size > 100)
      throw new IllegalArgumentException("页码或每页数量无效。");
    if (!List.of("", "dingtalk", "schedule").contains(type))
      throw new IllegalArgumentException("任务种类无效。");
    name = name.trim();
    if (name.length() > 255) throw new IllegalArgumentException("任务名称不能超过255个字符。");
    LocalDate start = date(startDate), end = date(endDate);
    if (start != null && end != null && start.isAfter(end))
      throw new IllegalArgumentException("开始日期不能晚于结束日期。");
    StringBuilder where = new StringBuilder(" WHERE r.triggerSource IN ('dingtalk', 'schedule')");
    Map<String, Object> args = new LinkedHashMap<>();
    GroupService.validateFilter(groupId);
    if ("unassigned".equals(groupId)) where.append(" AND r.groupId IS NULL");
    else if (groupId != null && !groupId.isEmpty()) {
      where.append(" AND r.groupId = :groupId");
      args.put("groupId", groupId);
    }
    if (!name.isEmpty()) {
      where.append(" AND LOWER(r.runName) LIKE LOWER(:name) ESCAPE '!'");
      args.put("name", "%" + name.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%");
    }
    if (!type.isEmpty()) {
      where.append(" AND r.triggerSource = :type");
      args.put("type", type);
    }
    ZoneId zone = ZoneId.of("Asia/Shanghai");
    if (start != null) {
      where.append(" AND r.submittedAt >= :start");
      args.put("start", start.atStartOfDay(zone).toInstant());
    }
    if (end != null) {
      where.append(" AND r.submittedAt < :end");
      args.put("end", end.plusDays(1).atStartOfDay(zone).toInstant());
    }
    var count = entities.createQuery("SELECT COUNT(r) FROM TaskRunEntity r" + where, Long.class);
    args.forEach(count::setParameter);
    var query =
        entities.createQuery(
            "SELECT r.workflowId, r.runName, r.triggerSource, r.status, r.submittedAt, r.groupId, r.groupName FROM TaskRunEntity r"
                + where
                + " ORDER BY r.submittedAt DESC, r.workflowId DESC",
            Object[].class);
    args.forEach(query::setParameter);
    query.setFirstResult(page * size).setMaxResults(size);
    ObjectNode result =
        json.newObject().put("total", count.getSingleResult()).put("page", page).put("size", size);
    var items = result.putArray("items");
    for (Object[] row : query.getResultList()) {
      String id = (String) row[0];
      items
          .addObject()
          .put("workflowId", id)
          .put("name", (String) row[1])
          .put("triggerSource", (String) row[2])
          .put("status", (String) row[3])
          .put("groupId", (String) row[5])
          .put("groupName", (String) row[6])
          .put("submittedAt", ((Instant) row[4]).toString())
          .put("monitorUrl", json.monitorUrl(id));
    }
    return result;
  }

  private static LocalDate date(String value) {
    if (value.isEmpty()) return null;
    try {
      if (!value.matches("\\d{4}-\\d{2}-\\d{2}")) throw new IllegalArgumentException();
      LocalDate parsed = LocalDate.parse(value);
      if (parsed.getYear() < 1970 || parsed.getYear() > 9998) throw new IllegalArgumentException();
      return parsed;
    } catch (RuntimeException error) {
      throw new IllegalArgumentException("日期无效，请使用1970至9998年之间的日期。");
    }
  }
}
