package com.labworks.levelsync;

import com.mojang.brigadier.arguments.StringArgumentType;
import net.fabricmc.api.DedicatedServerModInitializer;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.fabricmc.fabric.api.entity.event.v1.ServerEntityCombatEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.fabric.api.event.player.AttackEntityCallback;
import net.fabricmc.fabric.api.event.player.UseBlockCallback;
import net.fabricmc.fabric.api.event.player.UseItemCallback;
import net.minecraft.advancement.AdvancementEntry;
import net.minecraft.entity.Entity;
import net.minecraft.entity.mob.HostileEntity;
import net.minecraft.server.command.CommandManager;
import net.minecraft.server.network.ServerPlayerEntity;
import net.minecraft.text.Text;
import net.minecraft.util.ActionResult;
import net.minecraft.util.Hand;
import net.minecraft.util.TypedActionResult;
import net.minecraft.util.math.Vec3d;

public class LabworksLevelSync implements DedicatedServerModInitializer {
    private static LabworksLevelSync instance;

    private LabworksConfig config;
    private LabworksApiClient api;
    private final ProgressStore progressStore = new ProgressStore();
    private int tickCounter = 0;

    @Override
    public void onInitializeServer() {
        instance = this;
        config = LabworksConfig.load();
        api = new LabworksApiClient(config);
        progressStore.load();

        registerCommands();
        registerActivityHooks();
        registerMilestoneLoop();

        ServerLifecycleEvents.SERVER_STOPPING.register(server -> progressStore.save());
    }

    private void registerCommands() {
        CommandRegistrationCallback.EVENT.register((dispatcher, registryAccess, environment) -> {
            dispatcher.register(CommandManager.literal("linkdiscord")
                .then(CommandManager.argument("code", StringArgumentType.word())
                    .executes(context -> {
                        ServerPlayerEntity player = context.getSource().getPlayer();
                        if (player == null) {
                            return 0;
                        }

                        String code = StringArgumentType.getString(context, "code").toUpperCase();
                        markActive(player);
                        player.sendMessage(Text.literal("Sending link request to Discord..."), false);
                        api.sendLink(player, code).thenAccept(message -> {
                            if (player.getServer() != null) {
                                player.getServer().execute(() -> player.sendMessage(Text.literal(message), false));
                            }
                        });
                        return 1;
                    })));
        });
    }

    private void registerActivityHooks() {
        UseBlockCallback.EVENT.register((player, world, hand, hitResult) -> {
            if (!world.isClient && player instanceof ServerPlayerEntity serverPlayer) {
                markActive(serverPlayer);
            }
            return ActionResult.PASS;
        });

        UseItemCallback.EVENT.register((player, world, hand) -> {
            if (!world.isClient && player instanceof ServerPlayerEntity serverPlayer) {
                markActive(serverPlayer);
            }
            return TypedActionResult.pass(player.getStackInHand(hand));
        });

        AttackEntityCallback.EVENT.register((player, world, hand, entity, hitResult) -> {
            if (!world.isClient && player instanceof ServerPlayerEntity serverPlayer) {
                markActive(serverPlayer);
            }
            return ActionResult.PASS;
        });

        ServerEntityCombatEvents.AFTER_KILLED_OTHER_ENTITY.register((serverWorld, entity, killedEntity) -> {
            if (entity instanceof ServerPlayerEntity player && killedEntity instanceof HostileEntity) {
                PlayerProgress progress = progressStore.get(player.getUuid());
                progress.hostileKills += 1;
                markActive(player);

                int milestone = progress.hostileKills / config.hostileKillMilestone;
                if (milestone > progress.hostileKillMilestone) {
                    progress.hostileKillMilestone = milestone;
                    api.sendXpEvent(
                        player,
                        "hostile_kills",
                        "hostile_kills:" + (milestone * config.hostileKillMilestone),
                        config.hostileKillXp
                    );
                    progressStore.save();
                }
            }
        });

    }

    public static void onAdvancementCompleted(ServerPlayerEntity player, AdvancementEntry advancement) {
        if (instance == null || player == null || advancement == null) {
            return;
        }

        instance.rewardAdvancement(player, advancement);
    }

    private void rewardAdvancement(ServerPlayerEntity player, AdvancementEntry advancement) {
        String advancementId = advancement.id().toString();
        PlayerProgress progress = progressStore.get(player.getUuid());
        markActive(player);

        if (!progress.rewardedAdvancements.add(advancementId)) {
            return;
        }

        int xp = classifyAdvancementXp(advancement);
        api.sendXpEvent(player, "advancement", advancementId, xp);
        progressStore.save();
    }

    private void registerMilestoneLoop() {
        ServerTickEvents.END_SERVER_TICK.register(server -> {
            tickCounter += 1;
            if (tickCounter % 20 != 0) {
                return;
            }

            long now = System.currentTimeMillis();
            for (ServerPlayerEntity player : server.getPlayerManager().getPlayerList()) {
                PlayerProgress progress = progressStore.get(player.getUuid());
                updateDistance(player, progress);

                if (now - progress.lastActiveMillis > (long) config.afkTimeoutSeconds * 1000L) {
                    continue;
                }

                if (tickCounter % (20 * 60) == 0) {
                    progress.activeMinutes += 1;
                    if (progress.activeMinutes % config.activePlaytimeMinutes == 0) {
                        api.sendXpEvent(
                            player,
                            "active_playtime",
                            "active_playtime:" + progress.activeMinutes,
                            config.activePlaytimeXp
                        );
                        progressStore.save();
                    }
                }
            }

            if (tickCounter % (20 * 60) == 0) {
                progressStore.save();
            }
        });
    }

    private void updateDistance(ServerPlayerEntity player, PlayerProgress progress) {
        Vec3d pos = player.getPos();
        if (!progress.hasLastPosition) {
            progress.lastX = pos.x;
            progress.lastY = pos.y;
            progress.lastZ = pos.z;
            progress.hasLastPosition = true;
            return;
        }

        double dx = pos.x - progress.lastX;
        double dy = pos.y - progress.lastY;
        double dz = pos.z - progress.lastZ;
        double distance = Math.sqrt((dx * dx) + (dy * dy) + (dz * dz));

        progress.lastX = pos.x;
        progress.lastY = pos.y;
        progress.lastZ = pos.z;

        if (distance < 0.05 || distance > 40) {
            return;
        }

        progress.distanceTravelled += distance;
        markActive(player);

        int milestone = (int) (progress.distanceTravelled / config.distanceMilestoneBlocks);
        if (milestone > progress.distanceMilestone) {
            progress.distanceMilestone = milestone;
            api.sendXpEvent(
                player,
                "distance",
                "distance:" + (milestone * config.distanceMilestoneBlocks),
                config.distanceMilestoneXp
            );
        }

        int bonusMilestone = (int) (progress.distanceTravelled / config.distanceBonusEveryBlocks);
        if (bonusMilestone > progress.distanceBonusMilestone) {
            progress.distanceBonusMilestone = bonusMilestone;
            api.sendXpEvent(
                player,
                "distance_bonus",
                "distance_bonus:" + (bonusMilestone * config.distanceBonusEveryBlocks),
                config.distanceBonusXp
            );
        }
    }

    private void markActive(ServerPlayerEntity player) {
        PlayerProgress progress = progressStore.get(player.getUuid());
        progress.lastActiveMillis = System.currentTimeMillis();
    }

    private int classifyAdvancementXp(AdvancementEntry advancement) {
        String id = advancement.id().toString();
        if (id.contains("nether/summon_wither") || id.contains("end/kill_dragon") || id.contains("adventure/kill_all_mobs")) {
            return config.bossAdvancementXp;
        }
        if (id.startsWith("minecraft:nether/") || id.startsWith("minecraft:end/") || id.contains("adventure/")) {
            return config.majorAdvancementXp;
        }
        return config.normalAdvancementXp;
    }
}
