package com.example.codex.config;

import jakarta.persistence.*;
import org.springframework.data.jpa.repository.JpaRepository;
import java.time.Instant;
import java.util.*;

@MappedSuperclass
abstract class Timestamped {
    @Column(name="created_at", nullable=false) Instant createdAt;
    @Column(name="updated_at", nullable=false) Instant updatedAt;
    @PrePersist void createTimestamps() { var now=Instant.now(); createdAt=now; updatedAt=now; }
    @PreUpdate void updateTimestamp() { updatedAt=Instant.now(); }
}

@Entity @Table(name="codex_sop_roles")
class RoleEntity extends Timestamped {
    @Id String id;
    @Column(nullable=false, unique=true) String name;
    @Column(nullable=false, length=2000) String duty;
    @Column(nullable=false) boolean enabled=true;
    @Version long version;
}

@Entity @Table(name="codex_sop_sops")
class SopEntity extends Timestamped {
    @Id String id;
    @Column(nullable=false) String name;
    @Column(length=2000) String description;
    @Column(name="supervisor_agent_id", nullable=false) String supervisorAgentId="local";
    @Column(name="failure_policy", nullable=false) String failurePolicy="stop";
    @Column(name="supervisor_timeout_sec", nullable=false) int supervisorTimeoutSec=7200;
    @Column(name="default_step_model", nullable=false) String defaultStepModel="gpt-5.6-sol";
    @Column(nullable=false) boolean enabled=true;
    @OneToMany(mappedBy="sop", cascade=CascadeType.ALL, orphanRemoval=true)
    @OrderBy("positionNo ASC") List<SopStepEntity> steps=new ArrayList<>();
}

@Entity @Table(name="codex_sop_steps")
class SopStepEntity {
    @Id String id;
    @ManyToOne(fetch=FetchType.LAZY, optional=false) @JoinColumn(name="sop_id") SopEntity sop;
    @Column(name="position_no", nullable=false) int positionNo;
    @Column(name="display_name", nullable=false) String displayName;
    // ConfigService intentionally builds immutable JSON snapshots by reading entity
    // fields directly.  A lazy to-one association is a Hibernate proxy whose fields
    // are still null until a getter initializes it, so direct field access produced
    // null role IDs/names.  Keep snapshot relationships eagerly initialized.
    @ManyToOne(fetch=FetchType.EAGER, optional=false) @JoinColumn(name="role_id") RoleEntity role;
    @Column(name="instruction_text", nullable=false, columnDefinition="LONGTEXT") String instruction;
    @Column(name="expected_output", nullable=false, columnDefinition="LONGTEXT") String expectedOutput;
    @Column(name="executor_type", nullable=false) String executorType;
    @Column(name="agent_id", nullable=false) String agentId;
    @Column(name="working_directory") String workingDirectory;
    @Column(name="write_enabled", nullable=false) boolean writeEnabled;
    @Column(name="model_override") String modelOverride;
    @Column(name="timeout_sec", nullable=false) int timeoutSec=1800;
    @ElementCollection @CollectionTable(name="codex_sop_step_skills", joinColumns=@JoinColumn(name="step_id")) @Column(name="tag") Set<String> skills=new LinkedHashSet<>();
    @ElementCollection @CollectionTable(name="codex_sop_step_mcps", joinColumns=@JoinColumn(name="step_id")) @Column(name="tag") Set<String> mcps=new LinkedHashSet<>();
}

@Entity @Table(name="codex_sop_task_definitions")
class TaskDefinitionEntity extends Timestamped {
    @Id String id;
    @Column(nullable=false) String name;
    @Column(nullable=false, columnDefinition="LONGTEXT") String objective;
    @ManyToOne(fetch=FetchType.EAGER, optional=false) @JoinColumn(name="sop_id") SopEntity sop;
    @Column(name="additional_notes", columnDefinition="LONGTEXT") String additionalNotes;
    @Column(nullable=false) boolean enabled=true;
    @Column(nullable=false) boolean deleted=false;
}

@Entity @Table(name="codex_sop_task_runs")
class TaskRunEntity {
    @Id @Column(name="workflow_id") String workflowId;
    @ManyToOne(fetch=FetchType.EAGER, optional=false) @JoinColumn(name="task_definition_id") TaskDefinitionEntity taskDefinition;
    @Column(name="source_workflow_id") String sourceWorkflowId;
    @Column(nullable=false) String status;
    @Column(name="snapshot_json", nullable=false, columnDefinition="LONGTEXT") String snapshotJson;
    @Column(name="submitted_json", nullable=false, columnDefinition="LONGTEXT") String submittedJson;
    @Column(name="gateway_response_json", columnDefinition="LONGTEXT") String gatewayResponseJson;
    @Column(name="error_message", columnDefinition="LONGTEXT") String errorMessage;
    @Column(name="submitted_at", nullable=false) Instant submittedAt;
    @Column(name="updated_at", nullable=false) Instant updatedAt;
}

interface RoleRepository extends JpaRepository<RoleEntity,String> {
    List<RoleEntity> findByNameContainingIgnoreCaseOrderByCreatedAtDesc(String q);
    boolean existsByNameIgnoreCaseAndIdNot(String name,String id);
}
interface SopRepository extends JpaRepository<SopEntity,String> {
    List<SopEntity> findByNameContainingIgnoreCaseOrderByCreatedAtDesc(String q);
}
interface SopStepRepository extends JpaRepository<SopStepEntity,String> { boolean existsByRoleId(String roleId); }
interface TaskDefinitionRepository extends JpaRepository<TaskDefinitionEntity,String> {
    List<TaskDefinitionEntity> findByDeletedFalseAndNameContainingIgnoreCaseOrderByCreatedAtDesc(String q);
    boolean existsBySopIdAndDeletedFalse(String sopId);
}
interface TaskRunRepository extends JpaRepository<TaskRunEntity,String> {
    List<TaskRunEntity> findByTaskDefinitionIdOrderBySubmittedAtDesc(String taskDefinitionId);
}
