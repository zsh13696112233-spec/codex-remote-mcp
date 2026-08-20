package com.example.codex.console;

public class GatewayException extends RuntimeException {

    private final int statusCode;
    private final String responseBody;

    public GatewayException(int statusCode, String responseBody) {
        super("Codex 网关返回 HTTP " + statusCode);
        this.statusCode = statusCode;
        this.responseBody = responseBody;
    }

    public GatewayException(int statusCode, String message, Throwable cause) {
        super(message, cause);
        this.statusCode = statusCode;
        this.responseBody = message;
    }

    public int getStatusCode() {
        return statusCode;
    }

    public String getResponseBody() {
        return responseBody;
    }
}
