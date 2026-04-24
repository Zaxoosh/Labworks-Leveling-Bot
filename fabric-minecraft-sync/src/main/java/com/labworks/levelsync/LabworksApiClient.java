package com.labworks.levelsync;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import net.minecraft.server.network.ServerPlayerEntity;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Instant;
import java.util.concurrent.CompletableFuture;

public class LabworksApiClient {
    private static final Gson GSON = new Gson();

    private final LabworksConfig config;
    private final HttpClient client = HttpClient.newHttpClient();

    public LabworksApiClient(LabworksConfig config) {
        this.config = config;
    }

    public CompletableFuture<String> sendLink(ServerPlayerEntity player, String code) {
        JsonObject payload = basePayload(player, "link", "link:" + code, 0);
        payload.addProperty("code", code);
        return postForMessage(payload);
    }

    public void sendXpEvent(ServerPlayerEntity player, String eventType, String eventKey, int xp) {
        JsonObject payload = basePayload(player, eventType, eventKey, xp);
        post(payload);
    }

    private JsonObject basePayload(ServerPlayerEntity player, String eventType, String eventKey, int xp) {
        JsonObject payload = new JsonObject();
        payload.addProperty("minecraft_uuid", player.getUuidAsString());
        payload.addProperty("minecraft_name", player.getGameProfile().getName());
        payload.addProperty("event_type", eventType);
        payload.addProperty("event_key", eventKey);
        payload.addProperty("xp", xp);
        payload.addProperty("timestamp", Instant.now().getEpochSecond());
        return payload;
    }

    private void post(JsonObject payload) {
        if (config.apiToken == null || config.apiToken.isBlank() || config.apiToken.equals("change-me")) {
            return;
        }

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(config.apiUrl))
            .header("Content-Type", "application/json")
            .header("Authorization", "Bearer " + config.apiToken)
            .POST(HttpRequest.BodyPublishers.ofString(GSON.toJson(payload)))
            .build();

        client.sendAsync(request, HttpResponse.BodyHandlers.discarding());
    }

    private CompletableFuture<String> postForMessage(JsonObject payload) {
        if (config.apiToken == null || config.apiToken.isBlank() || config.apiToken.equals("change-me")) {
            return CompletableFuture.completedFuture("Minecraft sync is not configured yet: apiToken is still change-me.");
        }

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(config.apiUrl))
            .header("Content-Type", "application/json")
            .header("Authorization", "Bearer " + config.apiToken)
            .POST(HttpRequest.BodyPublishers.ofString(GSON.toJson(payload)))
            .build();

        return client.sendAsync(request, HttpResponse.BodyHandlers.ofString())
            .thenApply(response -> {
                if (response.statusCode() >= 200 && response.statusCode() < 300) {
                    return "Minecraft account linked successfully. Check /minecraftprofile in Discord.";
                }

                String body = response.body() == null ? "" : response.body();
                if (response.statusCode() == 401) {
                    return "Link failed: the Minecraft API token does not match the Discord bot.";
                }
                if (body.contains("invalid_code")) {
                    return "Link failed: that Discord code was not found. Run /linkminecraft again.";
                }
                if (body.contains("expired_code")) {
                    return "Link failed: that Discord code expired. Run /linkminecraft again.";
                }
                if (body.contains("missing_link_fields")) {
                    return "Link failed: the mod sent an incomplete link request.";
                }
                return "Link failed: bot returned HTTP " + response.statusCode() + ". Check the bot container logs.";
            })
            .exceptionally(error -> "Link failed: could not reach the Discord bot API at " + config.apiUrl + ".");
    }
}
