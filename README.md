# Segfault — Mech Royale bot

Team Segfault's entry for the Builders Buildathon Mech Royale track.

Pure Python 3 (standard library only). `Dockerfile` at the top builds the image; the entrypoint speaks `mechfight/1` on stdin/stdout.

## Files

| File | What it does |
| --- | --- |
| `bot.py` | Protocol loop + decision logic (zone positioning, loot, combat, dodging, upgrades). |
| `nav.py` | Walkability grid, incremental A*, line-of-sight path smoothing, cover search. |
| `navdata.py` | 2 m walkability grid of the arena, precomputed from the kit's `arena/v2/world.json` and `terrain.i16` (buildings/stadium taller than a step, steep terrain, map edge). Checksummed at load. |

## Strategy (short)

- Practice runs are 1 vs a 15-bot hunter squad, so score ≈ kills before death: fight efficiently, regenerate between fights.
- Lead every shot (target velocity, bullet speed), fire only when the actual torso aim is on target.
- Random strafe jinks + boost while fighting; duck out of line of sight during gun reloads and when low on HP.
- Focus the weakest visible threat; face attackers (rear hits take x1.5).
- Early game: sweep the view while walking to find weapon pickups (laser > lightning > mortar), farm crates/NPC drones for cubes when nobody is around, collect cubes after kills.
- Upgrades: armor > plating > nanites > gun rate/damage > ...
- Stay inside the next zone circle on a covered cell; stand still (silent) when idle.

## Open-source / pre-existing code

None besides the protocol described in the organizers' starter kit. `navdata.py` is derived from the kit's map files.
