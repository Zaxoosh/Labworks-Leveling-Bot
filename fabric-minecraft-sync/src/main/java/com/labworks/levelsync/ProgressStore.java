package com.labworks.levelsync;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.reflect.TypeToken;
import net.fabricmc.loader.api.FabricLoader;

import java.io.IOException;
import java.lang.reflect.Type;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;

public class ProgressStore {
    private static final Gson GSON = new GsonBuilder().setPrettyPrinting().create();
    private static final Type TYPE = new TypeToken<Map<UUID, PlayerProgress>>() {}.getType();

    private final Path path = FabricLoader.getInstance().getConfigDir().resolve("labworks-level-sync-state.json");
    private final Map<UUID, PlayerProgress> progress = new HashMap<>();

    public void load() {
        if (!Files.exists(path)) {
            return;
        }

        try {
            Map<UUID, PlayerProgress> loaded = GSON.fromJson(Files.readString(path), TYPE);
            if (loaded != null) {
                progress.putAll(loaded);
            }
        } catch (IOException e) {
            throw new RuntimeException("Failed to read Labworks Level Sync state", e);
        }
    }

    public PlayerProgress get(UUID uuid) {
        return progress.computeIfAbsent(uuid, ignored -> new PlayerProgress());
    }

    public void save() {
        try {
            Files.createDirectories(path.getParent());
            Files.writeString(path, GSON.toJson(progress, TYPE));
        } catch (IOException e) {
            throw new RuntimeException("Failed to write Labworks Level Sync state", e);
        }
    }
}
