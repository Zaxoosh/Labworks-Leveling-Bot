package com.labworks.levelsync;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import net.fabricmc.loader.api.FabricLoader;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

public class LabworksConfig {
    private static final Gson GSON = new GsonBuilder().setPrettyPrinting().create();

    public String apiUrl = "http://127.0.0.1:8095/minecraft/activity";
    public String apiToken = "change-me";
    public int afkTimeoutSeconds = 300;
    public int activePlaytimeMinutes = 10;
    public int activePlaytimeXp = 25;
    public int normalAdvancementXp = 100;
    public int majorAdvancementXp = 300;
    public int bossAdvancementXp = 500;
    public int distanceMilestoneBlocks = 1000;
    public int distanceMilestoneXp = 20;
    public int distanceBonusEveryBlocks = 5000;
    public int distanceBonusXp = 100;
    public int hostileKillMilestone = 50;
    public int hostileKillXp = 60;

    public static LabworksConfig load() {
        Path path = FabricLoader.getInstance().getConfigDir().resolve("labworks-level-sync.json");
        if (!Files.exists(path)) {
            LabworksConfig config = new LabworksConfig();
            config.save(path);
            return config;
        }

        try {
            return GSON.fromJson(Files.readString(path), LabworksConfig.class);
        } catch (IOException e) {
            throw new RuntimeException("Failed to read Labworks Level Sync config", e);
        }
    }

    private void save(Path path) {
        try {
            Files.createDirectories(path.getParent());
            Files.writeString(path, GSON.toJson(this));
        } catch (IOException e) {
            throw new RuntimeException("Failed to write Labworks Level Sync config", e);
        }
    }
}
