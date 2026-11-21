"""Segfault Mech Royale bot: survival-first.

Placement scores, so: stay inside the coming zone, hold quiet covered spots, face threats
(front armor), shoot back accurately, dodge telegraphed shots, take cheap loot early.
"""
import json
import math
import queue
import random
import sys
import threading
import time
import traceback

import nav

TAU = 2 * math.pi


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def unit(dx, dy):
    n = math.hypot(dx, dy)
    if n < 1e-9:
        return (0.0, 0.0)
    return (dx / n, dy / n)


import os
STRAFE_LO = float(os.environ.get("STRAFE_LO", 0.35))
STRAFE_HI = float(os.environ.get("STRAFE_HI", 0.8))
RETREAT_HP = float(os.environ.get("RETREAT_HP", 0.4))
COMBAT_BOOST = int(os.environ.get("COMBAT_BOOST", 1))
RELOAD_COVER = int(os.environ.get("RELOAD_COVER", 1))
LOOT_EARLY = float(os.environ.get("LOOT_EARLY", 200))
CUBE_RANGE = float(os.environ.get("CUBE_RANGE", 120))
HP_W = float(os.environ.get("HP_W", 0.08))
WANT_D = float(os.environ.get("WANT_D", 80))
LIGHT_D = float(os.environ.get("LIGHT_D", 40))
SWEEP = float(os.environ.get("SWEEP", 0.9))
PLAN_BUDGET = float(os.environ.get("PLAN_BUDGET", 0.012))
SPIN_T = float(os.environ.get("SPIN_T", 2.5))
CRATE_T = float(os.environ.get("CRATE_T", 0))
NPC_T = float(os.environ.get("NPC_T", 0))
LOS_GATE = int(os.environ.get("LOS_GATE", 1))
HIDE_HP = float(os.environ.get("HIDE_HP", 0.6))
NO_MORTAR = int(os.environ.get("NO_MORTAR", 0))
FIGHT_D = float(os.environ.get("FIGHT_D", 90))
KITE = float(os.environ.get("KITE", 0))
TACTICAL = int(os.environ.get("TACTICAL", 1))
EXP_W = float(os.environ.get("EXP_W", 1.0))
HIDE_REGEN = int(os.environ.get("HIDE_REGEN", 1))

UPGRADE_PREF = {
    "armor": 100, "plating": 95, "nanites": 80, "gun_rate": 60, "gun_damage": 58,
    "laser_charge": 55, "laser_damage": 54, "servos": 50, "gun_mag": 45, "gyros": 44,
    "sensors": 42, "mortar_reload": 40, "lightning_efficiency": 38, "capacitor": 36,
    "jets": 34, "reactor": 33, "gun_velocity": 32, "lightning_range": 30,
    "mortar_velocity": 25, "mortar_splash": 24, "lightning_chain": 20, "laser_pierce": 15,
}
WEAPON_PREF = {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0}


class Bot:
    name = "segfault"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.slot = hello.get("slot", 0)
        self.rng = random.Random(hello.get("seed", 1))
        m = self.cfg["mech"]
        self.step_up = m.get("step_up", 1.2)
        self.planner = None
        self.path = None
        self.path_goal = None
        self.path_i = 0
        self.plan_t = -99
        self.goal = None
        self.goal_phase = None
        self.enemies = {}      # id -> dict(pos, vel, t, hp, weapons, charging, aim)
        self.npcs = {}         # id -> (pos, t)
        self.attackers = {}    # id -> last time damaged us
        self.last_dmg_t = -99
        self.dmg_bearing = None
        self.dmg_bearing_t = -99
        self.sound_look = None  # (bearing, t)
        self.strafe_sign = 1
        self.strafe_t = 0
        self.stuck_n = 0
        self.escape_until = -1
        self.escape_dir = None
        self.last_pos = None
        self.scan_base = None
        self.crate_target = None
        self.laser_holding = False
        self.prev_t = 0
        self.dodge_until = -1
        self.dodge_dir = (0, 0)
        self.bad_pick = {}
        self.fails = {}

    # ------------------------------------------------------------------ helpers
    def zone_now(self, z, t):
        c, r = z["center"], z["radius"]
        nc, nr = z["next_center"], z["next_radius"]
        s, e = z["shrink_start"], z["shrink_end"]
        if t <= s or e <= s:
            return c, r
        f = min(1.0, (t - s) / (e - s))
        return [c[0] + (nc[0] - c[0]) * f, c[1] + (nc[1] - c[1]) * f], r + (nr - r) * f

    def choose_goal(self, me, z, t):
        nc, nr = z["next_center"], z["next_radius"]
        key = (round(nc[0]), round(nc[1]), round(nr))
        if self.goal is not None and self.goal_phase == key:
            return self.goal
        x, y = me["pos"][0], me["pos"][1]
        dx, dy = x - nc[0], y - nc[1]
        if math.hypot(dx, dy) < 1:
            a = self.rng.uniform(-math.pi, math.pi)
            dx, dy = math.cos(a), math.sin(a)
        ux, uy = unit(dx, dy)
        frac = 0.55 if nr > 60 else (0.35 if nr > 20 else 0.0)
        # if we are already well inside, stay roughly where we are
        dme = math.hypot(x - nc[0], y - nc[1])
        rad = min(dme, nr * frac) if dme < nr * 0.7 else nr * frac
        gx, gy = nc[0] + ux * rad, nc[1] + uy * rad
        # pick a covered free cell near that point, away from known NPCs
        best, bk = -1e9, None
        sr = max(4, min(14, int(nr * 0.12 / nav.C)))
        gk = nav.cell(gx, gy)
        gj, gi = divmod(gk, nav.NX)
        for dj in range(-sr, sr + 1, 2):
            for di in range(-sr, sr + 1, 2):
                i, j = gi + di, gj + dj
                if not (0 <= i < nav.NX and 0 <= j < nav.NY):
                    continue
                k = j * nav.NX + i
                if nav.BLK[k]:
                    continue
                cx, cy = nav.center(k)
                if math.hypot(cx - nc[0], cy - nc[1]) > nr * 0.85 and nr > 20:
                    continue
                sc = nav.cover_score(k) * 1.0 - 0.15 * math.hypot(di, dj)
                for (p, tt) in self.npcs.values():
                    if math.hypot(cx - p[0], cy - p[1]) < 55:
                        sc -= 30
                if sc > best:
                    best, bk = sc, k
        if bk is not None:
            gx, gy = nav.center(bk)
        self.goal = (gx, gy)
        self.goal_phase = key
        self.path = None
        self.planner = None
        return self.goal

    def plan_to(self, me, target, t, deadline):
        """Advance planning toward target; return a waypoint to steer at."""
        x, y = me["pos"][0], me["pos"][1]
        if self.path_goal is None or d2(self.path_goal, target) > 6 or t - self.plan_t > 8:
            if self.planner is None or self.planner.goal_xy != tuple(target):
                self.planner = nav.AStar(x, y, target[0], target[1], penalty=self.npc_penalty())
                self.plan_t = t
        if self.planner is not None and not self.planner.done:
            while time.perf_counter() < deadline:
                if self.planner.step(300):
                    break
            if self.planner.done:
                self.path = self.planner.path
                self.path_goal = tuple(target)
                self.path_i = 0
                self.planner = None
        if self.path:
            # advance along path: furthest waypoint with line of sight, within 40 m
            best = None
            for i in range(self.path_i, min(len(self.path), self.path_i + 8)):
                p = self.path[i]
                if d2(p, (x, y)) < 3.0:
                    self.path_i = i + 1
                    continue
                if nav.los(x, y, p[0], p[1]):
                    best = i
                elif best is not None:
                    break
            if best is not None:
                self.path_i = max(self.path_i, best - 1 if best > self.path_i else self.path_i)
                return self.path[best]
            if self.path_i < len(self.path):
                return self.path[self.path_i]
        return target

    def npc_penalty(self):
        homes = [p for (p, tt) in self.npcs.values()]
        if not homes:
            return None

        def pen(k):
            cx, cy = nav.center(k)
            for p in homes:
                if abs(cx - p[0]) < 45 and abs(cy - p[1]) < 45 and math.hypot(cx - p[0], cy - p[1]) < 45:
                    return 3.0
            return 0.0
        return pen

    def steer(self, x, y, dx, dy):
        """Rotate desired direction away from walls in the raw grid."""
        if dx == 0 and dy == 0:
            return (0.0, 0.0)
        a0 = math.atan2(dy, dx)
        for off in (0, 0.35, -0.35, 0.7, -0.7, 1.05, -1.05, 1.4, -1.4, 1.9, -1.9):
            a = a0 + off
            ok = True
            for r in (3.0, 5.0):
                if nav.blocked_at(x + math.cos(a) * r, y + math.sin(a) * r, nav.RAW):
                    ok = False
                    break
            if ok:
                return (math.cos(a), math.sin(a))
        return (math.cos(a0), math.sin(a0))

    # ----------------------------------------------------------------- aiming
    def lead(self, me, tgt, speed, cubes_t=0):
        ms = me["stats"].get("muzzle_height", 7.0)
        ox, oy, oz = me["pos"][0], me["pos"][1], me["pos"][2] + ms
        tp, tv = tgt["pos"], tgt.get("vel", [0, 0, 0])
        hz = 4.5 * (1 + 0.05 * (tgt.get("cubes") or 0)) if tgt.get("kind") == "mech" else 1.75
        px, py, pz = tp[0], tp[1], tp[2] + hz
        tt = 0.0
        for _ in range(3):
            d = math.sqrt((px - ox) ** 2 + (py - oy) ** 2 + (pz - oz) ** 2)
            tt = d / speed + 0.05
            px = tp[0] + tv[0] * tt
            py = tp[1] + tv[1] * tt
            vz = tv[2] if abs(tv[2]) > 0.5 else 0.0
            pz = tp[2] + hz + vz * tt
        yaw = math.atan2(py - oy, px - ox)
        pitch = math.atan2(pz - oz, math.hypot(px - ox, py - oy))
        return yaw, pitch, math.hypot(px - ox, py - oy)

    def mortar_aim(self, me, tgt, w):
        v, g = w.get("speed", 72), w.get("gravity", 22)
        ox, oy = me["pos"][0], me["pos"][1]
        oz = me["pos"][2] + me["stats"].get("eye_height", 8) + 1.5
        tp, tv = tgt["pos"], tgt.get("vel", [0, 0, 0])
        px, py, pz = tp[0], tp[1], tp[2] + 1
        th = None
        for _ in range(3):
            R = math.hypot(px - ox, py - oy)
            h = pz - oz
            disc = v ** 4 - g * (g * R * R + 2 * h * v * v)
            if disc < 0 or R < 1:
                return None
            th = math.atan2(v * v - math.sqrt(disc), g * R)  # low arc
            ft = R / (v * math.cos(th))
            px, py = tp[0] + tv[0] * ft, tp[1] + tv[1] * ft
        return math.atan2(py - oy, px - ox), th

    # ------------------------------------------------------------------- act
    def act(self, obs, deadline):
        t = obs["t"]
        me = obs["self"]
        x, y, z = me["pos"]
        mypos = (x, y)
        act = {}
        zone = obs["zone"]

        # offers
        off = obs.get("offer")
        if off:
            ch = off["choices"]
            best = max(range(len(ch)), key=lambda i: UPGRADE_PREF.get(ch[i]["id"], 10))
            act["pick"] = best

        # memory of actors
        for m in obs["mechs"]:
            if m["kind"] == "mech":
                self.enemies[m["id"]] = dict(m, t=t)
            else:
                if m["id"] not in self.npcs:
                    self.npcs[m["id"]] = (tuple(m["pos"]), t)
        for k in [k for k, e in self.enemies.items() if t - e["t"] > 6]:
            del self.enemies[k]
        visible_ids = {m["id"] for m in obs["mechs"]}

        for ev in obs["events"]:
            if ev["type"] == "damaged" and ev.get("by") is not None:
                self.attackers[ev["by"]] = t
                self.last_dmg_t = t
                if ev.get("bearing") is not None:
                    self.dmg_bearing = ev["bearing"]
                    self.dmg_bearing_t = t
            elif ev["type"] == "take_failed":
                pid = ev.get("id")
                self.fails[pid] = self.fails.get(pid, 0) + 1
                if self.fails[pid] >= 3 or ev.get("reason") in ("gone", "have"):
                    self.bad_pick[pid] = 1
            elif ev["type"] == "kill":
                self.enemies.pop(ev.get("target"), None)
        for s in obs.get("sounds", []):
            if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost", "jet") \
                    and s["dist"] in ("near", "mid"):
                self.sound_look = (s["bearing"], t)

        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        weapons = me["weapons"]
        energy = me["energy"]

        # ---------------- target selection
        enemy_list = [e for e in self.enemies.values()]
        threat = None
        best = 1e18
        for e in enemy_list:
            d = d2(e["pos"], mypos)
            sc = d * 0.6 + e.get("hp", 1400) * HP_W
            if t - self.attackers.get(e["id"], -99) < 4:
                sc -= 120
            if e["id"] not in visible_ids:
                sc += 60
            if sc < best:
                best, threat = sc, e
        npc_tgt = None
        for m in obs["mechs"]:
            if m["kind"] == "npc":
                d = d2(m["pos"], mypos)
                attacking = t - self.attackers.get(m["id"], -99) < 3
                if attacking or (d < 70 and not enemy_list and hp_frac > 0.6 and t < NPC_T):
                    if npc_tgt is None or d < d2(npc_tgt["pos"], mypos):
                        npc_tgt = m

        # ---------------- movement goal
        zc, zr = self.zone_now(zone, t)
        dist_edge = zr - math.hypot(x - zc[0], y - zc[1])
        goal = self.choose_goal(me, zone, t)
        nc, nr = zone["next_center"], zone["next_radius"]
        # time pressure: how long until the zone reaches us
        urgent = (not me["in_zone"]) or dist_edge < 15
        far_from_next = math.hypot(x - nc[0], y - nc[1]) > nr * 0.9

        move_target = goal
        mode = "goal"
        # loot: pickups
        slot1 = weapons[1] if len(weapons) > 1 else None
        best_p = None
        for p in obs.get("pickups", []):
            if p["kind"] == "gun" or (p["kind"] == "mortar" and NO_MORTAR):
                continue
            have = {w["kind"] for w in weapons if w}
            if p["kind"] in have:
                continue
            d = d2(p["pos"], mypos)
            better = slot1 is None or WEAPON_PREF.get(p["kind"], 0) > WEAPON_PREF.get(slot1["kind"], 0)
            if not better:
                continue
            if abs(p["pos"][2] - z) > 3.5 or p["id"] in self.bad_pick:
                continue
            lim = LOOT_EARLY if t < 120 else 80
            if p["kind"] == "laser" and t < 150:
                lim = max(lim, 260)
            if d < lim and (best_p is None or d < best_p[0]):
                best_p = (d, p)
        if best_p and not urgent and (threat is None or d2(threat["pos"], mypos) > 80):
            p = best_p[1]
            move_target = (p["pos"][0], p["pos"][1])
            mode = "loot"
            if best_p[0] < self.cfg["loot"].get("pickup_radius", 4) and abs(p["pos"][2] - z) < 4:
                act["take"] = p["id"]
                if slot1 is not None:
                    lw = weapons[1]
                    if not (lw["kind"] == "laser" and lw.get("charge", 0) > 0):
                        act["drop"] = 1
        # cubes
        if mode == "goal" and not urgent:
            bc = None
            for c in obs.get("cubes", []):
                d = d2(c["pos"], mypos)
                if d < CUBE_RANGE and abs(c["pos"][2] - z) < 3.5 and (bc is None or d < bc[0]):
                    bc = (d, c)
            vis_close = any(m["kind"] == "mech" and d2(m["pos"], mypos) < 60 for m in obs["mechs"])
            if bc and not vis_close:
                move_target = (bc[1]["pos"][0], bc[1]["pos"][1])
                mode = "cube"
        # supply drop: only if very close, landed and nobody around
        if mode == "goal" and not urgent and threat is None:
            for dr in obs.get("drops", []):
                d = d2(dr["pos"], mypos)
                if d < 90 and dr["lands_in"] < 6 and (dr.get("holder") in (None, me["id"])):
                    move_target = (dr["pos"][0], dr["pos"][1])
                    mode = "drop"
                    break

        # ---------------- combat decisions
        aim = None
        fire = [False, False]
        fight_tgt = None
        if threat is not None:
            d = d2(threat["pos"], mypos)
            attacked = t - self.attackers.get(threat["id"], -99) < 5
            ehp = threat.get("hp", 1400) / max(1, threat.get("max_hp", 1400))
            rv = 0.0
            if d > 1:
                rv = -((threat["vel"][0]) * (threat["pos"][0] - x) + (threat["vel"][1]) * (threat["pos"][1] - y)) / d
            approaching = rv > 3.0 and d < 170
            if threat["id"] in visible_ids and (d < FIGHT_D or attacked or approaching or (ehp < 0.35 and d < 180)):
                fight_tgt = threat
            elif threat["id"] in visible_ids or attacked:
                # watch it without shooting
                aim = [math.atan2(threat["pos"][1] - y, threat["pos"][0] - x), 0.0]
        if fight_tgt is None and npc_tgt is not None:
            fight_tgt = npc_tgt

        # crates (early, quiet otherwise)
        crate = None
        if fight_tgt is None and not self.enemies and not urgent and t < CRATE_T and mode in ("goal",):
            for c in obs.get("crates", []):
                d = d2(c["pos"], mypos)
                if 6 < d < 70 and abs(c["pos"][2] - z) < 10:
                    if crate is None or d < d2(crate["pos"], mypos):
                        crate = c

        mv = None
        boost = False
        jump = False
        if fight_tgt is not None:
            mv, boost = self.combat_move(me, fight_tgt, zone, t, hp_frac)
            aim, fire = self.shoot(me, fight_tgt, t, act)
            mode = "fight"
        elif crate is not None:
            cz = {"pos": [crate["pos"][0], crate["pos"][1], crate["pos"][2] - 0.5], "vel": [0, 0, 0], "kind": "crate"}
            yaw, pitch, _ = self.lead(me, cz, weapons[0]["stats"]["speed"] if weapons[0] else 252)
            aim = [yaw, pitch]
            if weapons[0] and weapons[0]["kind"] == "gun" and self.aim_ok(me, yaw, pitch, d2(crate["pos"], mypos), 1.0):
                if weapons[0].get("ammo", 0) > 0 and weapons[0].get("cooldown", 0) <= 0.1:
                    fire[0] = True
            mode = "crate"

        # projectile dodge (mortar landing / laser telegraph)
        self.dodge(obs, me, t)
        if t < self.dodge_until:
            mv = self.dodge_dir
            boost = energy > 15

        if mv is None and HIDE_REGEN and hp_frac < HIDE_HP and not urgent and mode in ("goal", "cube"):
            known = [e for e in self.enemies.values() if t - e["t"] < 6]
            if known:
                ex, ey = known[0]["pos"][0], known[0]["pos"][1]
                if nav.los(ex, ey, x, y, nav.RAW, 2.0):
                    cv = self.cover_dir(x, y, ex, ey)
                    if cv is not None:
                        mv = cv
                        mode = "hide"
                else:
                    mv = (0.0, 0.0)
                    mode = "hide"
            elif t - self.last_dmg_t < 8:
                mv = (0.0, 0.0)
                mode = "hide"
        if mv is None:
            # navigate
            wp = self.plan_to(me, move_target, t, deadline)
            dist_goal = d2(move_target, mypos)
            if mode == "goal" and dist_goal < 5 and not urgent:
                mv = (0.0, 0.0)  # hold still: silent
            else:
                dx, dy = wp[0] - x, wp[1] - y
                ux, uy = unit(dx, dy)
                sp = 1.0
                if mode == "goal" and dist_goal < 12:
                    sp = 0.4  # slow walk below step-sound speed
                mv = (ux * sp, uy * sp)
                if urgent and energy > 20:
                    boost = True
                elif far_from_next and t > zone["shrink_start"] - 15 and energy > 50:
                    boost = True

        # stuck detection / escape
        spd = math.hypot(me["vel"][0], me["vel"][1])
        want = math.hypot(mv[0], mv[1])
        if t < self.escape_until:
            mv = self.escape_dir
            jump = me["grounded"]
            boost = energy > 5
        elif want > 0.5 and spd < 1.2 and mode != "fight":
            self.stuck_n += 1
            if self.stuck_n > 8:
                self.stuck_n = 0
                a = math.atan2(mv[1], mv[0]) + self.rng.choice([-1, 1]) * self.rng.uniform(0.3, 1.2)
                self.escape_dir = (math.cos(a), math.sin(a)) if self.rng.random() < 0.5 else (mv[0], mv[1])
                self.escape_until = t + 1.2
                self.path = None
                self.planner = None
                self.path_goal = None
        else:
            self.stuck_n = max(0, self.stuck_n - 1)

        if mv != (0.0, 0.0) and t >= self.escape_until:
            mv = self.steer(x, y, mv[0], mv[1]) if want > 0.45 else mv
            if want <= 0.45:
                mv = (mv[0], mv[1])
            else:
                mv = (mv[0] * min(1.0, want), mv[1] * min(1.0, want))

        # keep energy reserve for jets / dodges
        if boost and energy < 12 and not urgent:
            boost = False

        # ---------------- aim when idle: watch threats / sounds / scan
        if aim is None:
            if t - self.dmg_bearing_t < 3 and self.dmg_bearing is not None:
                aim = [self.dmg_bearing, 0.0]
            elif self.sound_look and t - self.sound_look[1] < 2.5:
                aim = [self.sound_look[0], 0.0]
            elif t < SPIN_T and not self.enemies:
                aim = [wrap(me["aim"][0] + 1.2), 0.0]
            elif want > 0.3:
                aim = [math.atan2(mv[1], mv[0]) + SWEEP * math.sin(t * 2.2), 0.0]
            elif t < SPIN_T and not self.enemies:
                aim = [wrap(me["aim"][0] + 1.2), 0.0]
            else:
                if self.scan_base is None:
                    self.scan_base = math.atan2(zone["next_center"][1] - y, zone["next_center"][0] - x)
                aim = [wrap(self.scan_base + t * 0.9), 0.0]
        if want > 0.3 or fight_tgt is not None:
            self.scan_base = None

        # gun housekeeping: reload when empty-ish and calm
        g0 = weapons[0]
        if g0 and g0["kind"] == "gun" and g0.get("reload", 0) == 0:
            if g0.get("ammo", 0) == 0 or (g0.get("ammo", 99) < g0["stats"].get("mag", 12) * 0.5 and fight_tgt is None and crate is None):
                act["reload"] = True

        act.update(move=[round(mv[0], 3), round(mv[1], 3)], aim=[round(aim[0], 4), round(max(-0.69, min(1.29, aim[1])), 4)],
                   fire=fire, boost=bool(boost), jump=bool(jump))
        self.last_pos = mypos
        return act

    def aim_ok(self, me, yaw, pitch, dist, tol_m):
        ay, ap = me["aim"]
        tol = math.atan2(tol_m, max(dist, 1.0)) + 0.008
        return abs(wrap(ay - yaw)) < tol and abs(ap - pitch) < tol * 1.5

    def combat_move(self, me, tgt, zone, t, hp_frac):
        x, y = me["pos"][0], me["pos"][1]
        tx, ty = tgt["pos"][0], tgt["pos"][1]
        dx, dy = tx - x, ty - y
        d = math.hypot(dx, dy)
        ux, uy = unit(dx, dy)
        if t > self.strafe_t:
            self.strafe_sign = -self.strafe_sign if self.rng.random() < 0.75 else self.strafe_sign
            self.strafe_t = t + self.rng.uniform(STRAFE_LO, STRAFE_HI)
        px, py = -uy * self.strafe_sign, ux * self.strafe_sign
        is_npc = tgt.get("kind") == "npc"
        ehp = tgt.get("hp", 1400) / max(1, tgt.get("max_hp", 1400))
        want_d = WANT_D
        w1 = me["weapons"][1] if len(me["weapons"]) > 1 else None
        has_light = bool(w1 and w1["kind"] == "lightning")
        if has_light and me["energy"] > 20:
            want_d = LIGHT_D
        elif w1 and w1["kind"] == "mortar":
            want_d = 90
        if is_npc:
            want_d = 60
        retreat = hp_frac < RETREAT_HP and ehp > hp_frac + 0.1 and not is_npc
        radial = 0.0
        if retreat:
            radial = -1.0
        elif d < want_d * 0.6:
            radial = -0.6
        elif d > want_d * 1.4:
            radial = 0.5
        nfoes = sum(1 for e in self.enemies.values() if t - e["t"] < 2 and d2(e["pos"], (x, y)) < 200)
        if KITE and nfoes >= 2 and not is_npc and ehp > 0.3:
            # back away from the group's centroid to stagger their arrival
            gx = sum(e["pos"][0] for e in self.enemies.values() if t - e["t"] < 2) / max(1, nfoes)
            gy = sum(e["pos"][1] for e in self.enemies.values() if t - e["t"] < 2) / max(1, nfoes)
            kx, ky = unit(x - gx, y - gy)
            px, py = px * 0.6 + kx * KITE, py * 0.6 + ky * KITE
        mx, my = px * 0.8 + ux * radial, py * 0.8 + uy * radial
        # zone constraint: pull toward next center if near the edge
        zc, zr = self.zone_now(zone, t)
        de = zr - math.hypot(x - zc[0], y - zc[1])
        if de < 25:
            cx, cy = unit(zc[0] - x, zc[1] - y)
            mx, my = mx + cx * 1.5, my + cy * 1.5
        g0 = me["weapons"][0]
        reloading = g0 is not None and g0.get("reload", 0) > 0.4 and not is_npc
        if (retreat or (reloading and RELOAD_COVER)) and d > 25:
            cv = self.cover_dir(x, y, tx, ty)
            if cv is not None:
                mx, my = cv[0] * 1.5 + mx * 0.5, cv[1] * 1.5 + my * 0.5
        mx, my = unit(mx, my)
        if TACTICAL and not is_npc:
            tv = self.tactical(me, tgt, t, (mx, my), retreat or reloading)
            if tv is not None:
                mx, my = tv
        boost = (retreat and me["energy"] > 15) or (COMBAT_BOOST and me["energy"] > 45 and not is_npc and not has_light)
        return (mx, my), boost

    def tactical(self, me, tgt, t, pref, hide):
        x, y = me["pos"][0], me["pos"][1]
        foes = [e for e in self.enemies.values() if t - e["t"] < 3 and d2(e["pos"], (x, y)) < 240]
        if len(foes) < 1:
            return None
        best = None
        for k in range(12):
            a = k * TAU / 12
            ca, sa = math.cos(a), math.sin(a)
            for r in (5.0,):
                px, py = x + ca * r, y + sa * r
                if nav.blocked_at(px, py) or not nav.los(x, y, px, py, nav.RAW):
                    continue
                exp = 0.0
                tgt_los = False
                for e in foes:
                    ex, ey = e["pos"][0], e["pos"][1]
                    if nav.los(ex, ey, px, py, nav.RAW, 2.0):
                        exp += 1.0
                        if e["id"] == tgt["id"]:
                            tgt_los = True
                sc = -EXP_W * exp
                if not hide:
                    sc += (EXP_W * 0.8) if tgt_los else -EXP_W * 0.5
                sc += 1.0 * (ca * pref[0] + sa * pref[1])
                if best is None or sc > best[0]:
                    best = (sc, ca, sa)
        if best is None:
            return None
        return (best[1], best[2])

    def cover_dir(self, x, y, tx, ty):
        best = None
        for k in range(16):
            a = k * TAU / 16
            for r in (12.0, 22.0):
                px, py = x + math.cos(a) * r, y + math.sin(a) * r
                if not nav.los(x, y, px, py, nav.RAW):
                    break
                if nav.blocked_at(px, py):
                    continue
                if not nav.los(tx, ty, px, py, nav.RAW, 2.0):
                    sc = r + 0.3 * math.hypot(px - tx, py - ty)
                    if best is None or sc < best[0]:
                        best = (sc, math.cos(a), math.sin(a))
                    break
        if best is None:
            return None
        return (best[1], best[2])

    def shoot(self, me, tgt, t, act):
        weapons = me["weapons"]
        x, y = me["pos"][0], me["pos"][1]
        d = d2(tgt["pos"], (x, y))
        fire = [False, False]
        aim = None
        g0 = weapons[0]
        w1 = weapons[1] if len(weapons) > 1 else None
        ehp = tgt.get("hp", 1400)
        clear = (not LOS_GATE) or nav.los(x, y, tgt["pos"][0], tgt["pos"][1], nav.RAW, 2.0)
        # slot 1 special weapons
        if w1 is not None:
            k = w1["kind"]
            st = w1["stats"]
            if k == "laser" and d < st.get("range", 280) * 0.95:
                yaw, pitch, _ = self.lead(me, tgt, 1e6)
                aim = [yaw, pitch]
                ch = w1.get("charge", 0)
                if ch > 0 or self.laser_holding:
                    full = st.get("charge", 0.75)
                    if ch >= full * 0.97 and clear and self.aim_ok(me, yaw, pitch, d, 1.6):
                        self.laser_holding = False  # release -> fire
                    elif ch >= full + 1.5:
                        self.laser_holding = False
                    else:
                        fire[1] = True
                        self.laser_holding = True
                elif w1.get("cooldown", 0) <= 0 and abs(wrap(me["aim"][0] - yaw)) < 0.3:
                    fire[1] = True
                    self.laser_holding = True
            if k == "mortar" and 25 < d < 225 and w1.get("cooldown", 0) <= 0:
                ma = self.mortar_aim(me, tgt, st)
                if ma:
                    aim = [ma[0], ma[1]]
                    if abs(wrap(me["aim"][0] - ma[0])) < 0.03 and abs(me["aim"][1] - ma[1]) < 0.03:
                        fire[1] = True
                    return aim, fire
            if k == "lightning" and d < st.get("range", 45) * 0.92 and me["energy"] > 15:
                yaw = math.atan2(tgt["pos"][1] - y, tgt["pos"][0] - x)
                yaw2, pitch, _ = self.lead(me, tgt, 1e6)
                aim = [yaw, pitch]
                if clear and abs(wrap(me["aim"][0] - yaw)) < math.radians(st.get("cone_deg", 30)) * 0.4 and w1.get("cooldown", 0) <= 0:
                    fire[1] = True
        if g0 is not None and g0["kind"] == "gun":
            st = g0["stats"]
            yaw, pitch, hd = self.lead(me, tgt, st.get("speed", 252))
            if aim is None:
                aim = [yaw, pitch]
            if hd < st.get("range", 230) * 0.97 and g0.get("ammo", 0) > 0 and g0.get("reload", 0) == 0:
                tol = 2.2 if tgt.get("kind") == "mech" else 1.4
                if clear and self.aim_ok(me, yaw, pitch, hd, tol) and g0.get("cooldown", 0) <= 0.1:
                    fire[0] = True
        if aim is None:
            aim = [math.atan2(tgt["pos"][1] - y, tgt["pos"][0] - x), 0.0]
        return aim, fire

    def dodge(self, obs, me, t):
        x, y, z = me["pos"]
        g = self.cfg["weapons"]["mortar"].get("gravity", 22)
        for p in obs.get("projectiles", []):
            if p["kind"] != "mortar":
                continue
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            # time until shell reaches our height (z + 2)
            a, b, c = -0.5 * g, vz, pz - (z + 2)
            disc = b * b - 4 * a * c
            if disc < 0:
                continue
            tt = (-b - math.sqrt(disc)) / (2 * a)
            if tt < 0 or tt > 4:
                continue
            lx, ly = px + vx * tt, py + vy * tt
            if math.hypot(lx - x, ly - y) < 11:
                ux, uy = unit(x - lx, y - ly)
                if ux == 0 and uy == 0:
                    ux, uy = unit(-vy, vx)
                self.dodge_dir = (ux, uy)
                self.dodge_until = t + min(1.0, tt + 0.2)
                return
        for m in obs["mechs"]:
            if m["kind"] == "mech" and m.get("charging"):
                d = d2(m["pos"], (x, y))
                yaw_to_me = math.atan2(y - m["pos"][1], x - m["pos"][0])
                if abs(wrap(m["aim"][0] - yaw_to_me)) < 0.25 and d < 290:
                    ux, uy = unit(x - m["pos"][0], y - m["pos"][1])
                    s = self.strafe_sign
                    self.dodge_dir = (-uy * s, ux * s)
                    self.dodge_until = t + 0.3
                    return


def energy_ok(me):
    return me["energy"] > 25


# ---------------------------------------------------------------------- runtime
def _send(msg):
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _reader(q):
    for line in sys.stdin:
        if line.strip():
            q.put(line)
    q.put(None)


def main():
    q = queue.Queue()
    threading.Thread(target=_reader, args=(q,), daemon=True).start()
    bot = None
    last = {}
    errors = 0
    while True:
        batch = [q.get()]
        t0 = time.perf_counter()
        while True:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        latest = None
        for raw in batch:
            if raw is None:
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "hello":
                try:
                    bot = Bot(msg)
                except Exception:
                    log(traceback.format_exc())
                _send({"type": "ready", "name": "segfault"})
            elif kind == "obs":
                latest = msg
            elif kind == "end":
                return
        if latest is None:
            continue
        action = {}
        if bot is not None:
            try:
                action = dict(bot.act(latest, t0 + PLAN_BUDGET) or {})
                last = {k: action[k] for k in ("move", "aim", "boost") if k in action}
            except Exception:
                errors += 1
                if errors <= 5 or errors % 100 == 0:
                    log(f"act() failed at tick {latest.get('tick')}:\n{traceback.format_exc()}")
                action = dict(last)
        action["tick"] = latest["tick"]
        try:
            _send(action)
        except (TypeError, ValueError):
            _send({"tick": latest["tick"], **last})


if __name__ == "__main__":
    main()
