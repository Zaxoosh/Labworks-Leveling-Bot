package com.labworks.levelsync;

import java.util.HashSet;
import java.util.Set;

public class PlayerProgress {
    public double lastX;
    public double lastY;
    public double lastZ;
    public boolean hasLastPosition;
    public long lastActiveMillis;
    public int activeMinutes;
    public double distanceTravelled;
    public int distanceMilestone;
    public int distanceBonusMilestone;
    public int hostileKills;
    public int hostileKillMilestone;
    public Set<String> rewardedAdvancements = new HashSet<>();
}
