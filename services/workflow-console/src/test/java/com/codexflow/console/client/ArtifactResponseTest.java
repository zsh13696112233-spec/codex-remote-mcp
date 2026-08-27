package com.codexflow.console.client;

import static org.assertj.core.api.Assertions.assertThat;

import com.codexflow.console.web.WorkflowController;
import java.net.http.HttpClient;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpHeaders;
import org.springframework.http.ResponseEntity;
import tools.jackson.databind.ObjectMapper;

/** 验证图片预览与普通文件下载的安全响应头。 */
class ArtifactResponseTest {

  @Test
  void nonImageIsForcedToDownloadWithNosniff() {
    WorkflowController controller =
        new WorkflowController(
            new StubGatewayClient(
                new GatewayClient.BinaryResponse("<html>".getBytes(), "text/html", "report.html")));

    ResponseEntity<byte[]> response = controller.artifact("workflow", "artifact");

    assertThat(response.getHeaders().getFirst("X-Content-Type-Options")).isEqualTo("nosniff");
    assertThat(response.getHeaders().getFirst(HttpHeaders.CONTENT_DISPOSITION))
        .startsWith("attachment;")
        .contains("report.html");
  }

  @Test
  void imageRemainsInlinePreview() {
    WorkflowController controller =
        new WorkflowController(
            new StubGatewayClient(
                new GatewayClient.BinaryResponse(new byte[] {1}, "image/png", "preview.png")));

    ResponseEntity<byte[]> response = controller.artifact("workflow", "artifact");

    assertThat(response.getHeaders().getFirst(HttpHeaders.CONTENT_DISPOSITION))
        .startsWith("inline;");
  }

  private static final class StubGatewayClient extends GatewayClient {
    private final BinaryResponse artifact;

    private StubGatewayClient(BinaryResponse artifact) {
      super(new ObjectMapper(), "http://127.0.0.1:1", HttpClient.newHttpClient());
      this.artifact = artifact;
    }

    @Override
    public BinaryResponse artifact(String workflowId, String artifactId) {
      return artifact;
    }
  }
}
