package com.codexflow.configcenter.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.*;

import java.time.Instant;
import java.util.Optional;
import org.junit.jupiter.api.Test;

class WorkflowSubmissionRecoveryTest {
  @Test
  void recoveryUsesPersistedAgeAndOnlyPendingSubmissions() {
    TaskRunRepository runs = mock(TaskRunRepository.class);
    WorkflowRunStore store = new WorkflowRunStore(null, runs, null, null, null);
    TaskRunEntity run = new TaskRunEntity();
    run.status = "submitting";
    run.submittedAt = Instant.now();
    when(runs.findById("run")).thenReturn(Optional.of(run));
    assertThat(store.submissionRecoveryDue("run")).isFalse();
    run.submittedAt = Instant.now().minusSeconds(61);
    assertThat(store.submissionRecoveryDue("run")).isTrue();
    for (String status : new String[] {"queued", "running", "completed", "submit_failed"}) {
      run.status = status;
      assertThat(store.submissionRecoveryDue("run")).isFalse();
    }
  }
}
