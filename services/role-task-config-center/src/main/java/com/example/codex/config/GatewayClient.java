package com.example.codex.config;

import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.time.Duration;

@Component
public class GatewayClient {
    private final ObjectMapper mapper; private final HttpClient client; private final String base;
    GatewayClient(ObjectMapper mapper,@Value("${codex.gateway.base-url}") String base) {
        this.mapper=mapper; this.base=base.replaceAll("/+$","");
        this.client=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    }
    JsonNode get(String path) { return exchange("GET",path,null); }
    JsonNode post(String path,JsonNode body) { return exchange("POST",path,body); }
    private JsonNode exchange(String method,String path,JsonNode body) {
        try {
            var b=HttpRequest.newBuilder(URI.create(base+path)).timeout(Duration.ofSeconds(30)).header("Accept","application/json");
            if(body==null)b.method(method,HttpRequest.BodyPublishers.noBody());
            else b.header("Content-Type","application/json").method(method,HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body),StandardCharsets.UTF_8));
            var response=client.send(b.build(),HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            if(response.statusCode()<200||response.statusCode()>=300) throw new GatewayFailure(response.statusCode(),response.body());
            return response.body().isBlank()?mapper.createObjectNode():mapper.readTree(response.body());
        } catch(GatewayFailure e){ throw e; } catch(Exception e){ if(e instanceof InterruptedException) Thread.currentThread().interrupt(); throw new GatewayFailure(502,e.getMessage()); }
    }
}
class GatewayFailure extends RuntimeException { final int status; GatewayFailure(int status,String message){super(message);this.status=status;} }
class NotFoundFailure extends RuntimeException { NotFoundFailure(String message){super(message);} }
class ConflictFailure extends RuntimeException { ConflictFailure(String message){super(message);} }
