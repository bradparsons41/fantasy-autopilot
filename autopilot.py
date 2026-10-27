#!/usr/bin/env python3
"""
ESPN Fantasy Basketball autopilot.

Each run:
  1. Loads your roster and today's NBA schedule.
  2. Benches starters ruled OUT (or another status you list) and starts the
     best healthy bench players who have a game today. Players whose game has
     already tipped off are locked by ESPN and left alone.
  3. Optionally picks up a free agent (streaming or upgrade mode), dropping
     only players you have explicitly marked as droppable.
  4. Sends a push notification (ntfy.sh) describing anything it did.

Runs in DRY-RUN mode unless you pass --live.

Reads use the open-source `espn_api` package. Writes (lineup moves, adds and
drops) use ESPN's internal transactions endpoint, which is unofficial and
reverse-engineered: ESPN can change it without notice.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

import requests

BENCH, IR = 12, 13
NON_ACTIVE = {BENCH, IR}
WRITE_URL = (
    "https://lm-api-writes.fantasy.espn.com/apis/v3/games/fba/seasons/{year}"
    "/segments/0/leagues/{league_id}/transactions/"
)
DAY_MS = 86_400_000

DEFAULTS = {
    "lineup": {
        "enabled": True,
        "strategy": "optimize",  # or "injuries_only"
        "bench_statuses": ["OUT", "SUSPENSION", "INJURY_RESERVE"],
        "lock_buffer_minutes": 5,
    },
    "value": {"source": "blend"},
    "waivers": {
        "enabled": False,
        "mode": "stream",  # or "upgrade"
        "droppable": [],
        "min_upgrade": 0.0,
        "max_adds_per_week": 4,
        "claim_waivers": False,
        "faab_bid": 0,
        "pool_size": 100,
    },
    "notify": {"ntfy_topic": "", "notify_on_no_changes": False},
}


# ---------------------------------------------------------------------------
# Plain data model (keeps the decision logic testable without ESPN)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class P:
    id: int
    name: str
    slot: int | None          # current lineup slot id; None for free agents
    eligible: frozenset[int]  # lineup slot ids the player can fill
    value: float              # higher is better
    status: str               # ESPN injuryStatus, e.g. ACTIVE, DAY_TO_DAY, OUT
    game_ms: int | None       # today's tip-off (epoch ms); None if no game today
    pool_status: str = ""     # FREEAGENT or WAIVERS for free-agent pool players


@dataclass
class Plan:
    moves: list[tuple[P, int, int]] = field(default_factory=list)  # (player, from, to)
    starters: dict[int, int] = field(default_factory=dict)         # player id -> slot
    empty: list[int] = field(default_factory=list)                 # unfilled slot ids
    too_late: list[P] = field(default_factory=list)                # OUT but locked in


@dataclass
class Pickup:
    add: P
    drop: P | None
    waiver: bool
    reason: str


def is_locked(p: P, now_ms: int, buffer_ms: int) -> bool:
    return p.game_ms is not None and now_ms >= p.game_ms - buffer_ms


def is_healthy(p: P, bad: set[str]) -> bool:
    return (p.status or "ACTIVE") not in bad


# ---------------------------------------------------------------------------
# Lineup decisions
# ---------------------------------------------------------------------------
def plan_lineup(roster: list[P], slot_counts: dict[int, int], now_ms: int, lcfg: dict) -> Plan:
    """Pick the highest-value legal lineup for today, respecting locks.

    Greedy-by-value with augmenting paths is optimal here: the sets of players
    that can be seated together form a transversal matroid.
    """
    buf = int(lcfg["lock_buffer_minutes"]) * 60_000
    bad = set(lcfg["bench_statuses"])
    injuries_only = lcfg.get("strategy") == "injuries_only"

    slots = [s for s in sorted(slot_counts) if s not in NON_ACTIVE for _ in range(slot_counts[s])]
    match: dict[int, P] = {}
    plan = Plan()

    # 1. Players that must stay where they are: locked starters, and in
    #    injuries_only mode, healthy starters who play today.
    pinned: set[int] = set()
    for p in roster:
        if p.slot is None or p.slot in NON_ACTIVE:
            continue
        locked = is_locked(p, now_ms, buf)
        keep = locked or (injuries_only and p.game_ms is not None and is_healthy(p, bad))
        if locked and p.game_ms is not None and not is_healthy(p, bad):
            plan.too_late.append(p)
        if keep:
            idx = next((i for i, s in enumerate(slots) if s == p.slot and i not in match), None)
            if idx is not None:
                match[idx] = p
                pinned.add(p.id)
    fixed_idx = set(match)
    open_idx = [i for i in range(len(slots)) if i not in fixed_idx]

    # 2. Everyone else who could start today.
    cands = [
        p for p in roster
        if p.id not in pinned
        and p.slot != IR
        and p.game_ms is not None
        and not is_locked(p, now_ms, buf)
        and is_healthy(p, bad)
    ]

    def assign(p: P, m: dict[int, P], seen: set[int]) -> bool:
        order = sorted(open_idx, key=lambda i: slots[i] != p.slot)  # prefer current slot
        for i in order:
            if slots[i] in p.eligible and i not in seen:
                seen.add(i)
                if i not in m or assign(m[i], m, seen):
                    m[i] = p
                    return True
        return False

    chosen: dict[int, P] = {}
    for p in sorted(cands, key=lambda p: (-p.value, p.slot in NON_ACTIVE)):
        trial = dict(chosen)
        if assign(p, trial, set()):
            chosen = trial

    # 3. Re-seat the chosen group so players already starting move as little as possible.
    final: dict[int, P] = {}
    for p in sorted(chosen.values(), key=lambda p: p.slot in NON_ACTIVE or p.slot is None):
        assign(p, final, set())
    match.update(final)

    plan.starters = {p.id: slots[i] for i, p in match.items()}
    plan.empty = [slots[i] for i in range(len(slots)) if i not in match]

    # 4. Translate into moves.
    for p in roster:
        if p.id in pinned or p.slot is None or p.slot == IR or is_locked(p, now_ms, buf):
            continue
        target = plan.starters.get(p.id, BENCH)
        if target != p.slot:
            plan.moves.append((p, p.slot, target))
    return plan


# ---------------------------------------------------------------------------
# Free-agent decisions
# ---------------------------------------------------------------------------
def _norm(name: str) -> str:
    return " ".join(name.lower().replace(".", "").split())


def plan_pickup(
    roster: list[P],
    pool: list[P],
    slot_counts: dict[int, int],
    now_ms: int,
    cfg: dict,
    base: Plan,
) -> Pickup | None:
    wcfg, lcfg = cfg["waivers"], cfg["lineup"]
    buf = int(lcfg["lock_buffer_minutes"]) * 60_000
    bad = set(lcfg["bench_statuses"])
    roster_limit = sum(n for s, n in slot_counts.items() if s != IR)
    active_roster = [p for p in roster if p.slot != IR]
    need_drop = len(active_roster) >= roster_limit

    droppable = {_norm(n) for n in wcfg["droppable"]}
    drops = sorted(
        (
            p for p in active_roster
            if _norm(p.name) in droppable
            and not is_locked(p, now_ms, buf)
            and p.id not in base.starters
        ),
        key=lambda p: p.value,
    )
    if need_drop and not drops:
        return None
    drop = drops[0] if need_drop else None
    floor = (drop.value if drop else float("-inf")) + float(wcfg["min_upgrade"])

    stream = wcfg["mode"] == "stream"
    if stream and not base.empty:
        return None  # every starting slot is already filled tonight

    for fa in sorted(pool, key=lambda p: -p.value):
        if not is_healthy(fa, bad) or fa.value <= floor:
            continue
        waiver = fa.pool_status == "WAIVERS"
        if waiver and (stream or not wcfg["claim_waivers"]):
            continue
        if stream:
            if fa.game_ms is None or is_locked(fa, now_ms, buf):
                continue
            sim = [p for p in roster if not drop or p.id != drop.id] + [replace(fa, slot=BENCH)]
            if len(plan_lineup(sim, slot_counts, now_ms, lcfg).empty) >= len(base.empty):
                continue  # wouldn't actually fill an open slot tonight
            reason = "fills an empty starting slot tonight"
        else:
            reason = f"value {fa.value:.1f} vs {drop.value:.1f}" if drop else "open roster spot"
        return Pickup(add=fa, drop=drop, waiver=waiver, reason=reason)
    return None


# ---------------------------------------------------------------------------
# ESPN I/O
# ---------------------------------------------------------------------------
class ESPN:
    def __init__(self, cfg: dict):
        from espn_api.basketball import League  # imported lazily so tests run without it

        e = cfg["espn"]
        self.league_id = int(e["league_id"])
        self.team_id = int(e["team_id"])
        self.year = int(e["year"])
        self.s2 = os.environ.get("ESPN_S2") or e.get("espn_s2", "")
        self.swid = os.environ.get("ESPN_SWID") or e.get("swid", "")
        if not self.s2 or not self.swid:
            raise SystemExit("Missing ESPN_S2 / ESPN_SWID (see README).")
        if not self.swid.startswith("{"):
            self.swid = "{" + self.swid.strip("{}") + "}"
        self.League = League
        self.vcfg = cfg["value"]
        self.load()

    def load(self) -> None:
        self.league = self.League(self.league_id, self.year, espn_s2=self.s2, swid=self.swid)
        self.sp = self.league.scoringPeriodId
        self.team = self.league.get_team_data(self.team_id)
        if self.team is None:
            raise SystemExit(f"team_id {self.team_id} not found in league {self.league_id}")

    # --- reads -------------------------------------------------------------
    def slot_counts(self) -> dict[int, int]:
        data = self.league.espn_request.league_get(params={"view": "mSettings"})
        counts = data["settings"]["rosterSettings"]["lineupSlotCounts"]
        return {int(k): int(v) for k, v in counts.items() if int(v) > 0}

    def roster(self) -> list[P]:
        return [self._to_p(pl) for pl in self.team.roster]

    def free_agent_pool(self, size: int) -> list[P]:
        from espn_api.basketball.player import Player

        filters = {
            "players": {
                "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
                "limit": size,
                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            }
        }
        data = self.league.espn_request.league_get(
            params={"view": "kona_player_info", "scoringPeriodId": self.sp},
            headers={"x-fantasy-filter": json.dumps(filters)},
        )
        out = []
        for raw in data.get("players", []):
            try:
                pl = Player(raw, self.year)
            except Exception:
                continue
            out.append(self._to_p(pl, pool_status=raw.get("status", "FREEAGENT"), free_agent=True))
        return out

    def adds_last_7_days(self) -> int:
        cutoff = time.time() * 1000 - 7 * DAY_MS
        n = 0
        for act in self.league.recent_activity(size=50):
            if act.date < cutoff:
                break
            for team, action, *_ in act.actions:
                if getattr(team, "team_id", None) == self.team_id and action in ("FA ADDED", "WAIVER ADDED"):
                    n += 1
        return n

    def _to_p(self, pl, pool_status: str = "", free_agent: bool = False) -> P:
        from espn_api.basketball.constant import POSITION_MAP, PRO_TEAM_MAP

        team_ids = {abbr: tid for tid, abbr in PRO_TEAM_MAP.items() if isinstance(tid, int)}
        games = self.league.pro_schedule.get(team_ids.get(pl.proTeam), {}).get(str(self.sp))
        slot = None if free_agent else POSITION_MAP.get(pl.lineupSlot)
        eligible = frozenset(POSITION_MAP[s] for s in pl.eligibleSlots if s in POSITION_MAP)
        return P(
            id=pl.playerId,
            name=pl.name,
            slot=slot,
            eligible=eligible,
            value=player_value(pl, self.vcfg),
            status=pl.injuryStatus or "ACTIVE",
            game_ms=games[0]["date"] if games else None,
            pool_status=pool_status,
        )

    # --- writes ------------------------------------------------------------
    def _payload(self, txn_type: str, items: list[dict], **extra) -> dict:
        return {
            "isLeagueManager": False,
            "teamId": self.team_id,
            "type": txn_type,
            "memberId": self.swid,
            "scoringPeriodId": self.sp,
            "executionType": "EXECUTE",
            "items": items,
            **extra,
        }

    def lineup_payload(self, moves: list[tuple[P, int, int]]) -> dict:
        items = [
            {"playerId": p.id, "type": "LINEUP", "fromLineupSlotId": f, "toLineupSlotId": t}
            for p, f, t in moves
        ]
        return self._payload("ROSTER", items)

    def pickup_payload(self, pk: Pickup, bid: int) -> dict:
        items = [{"playerId": pk.add.id, "type": "ADD", "toTeamId": self.team_id}]
        if pk.drop:
            items.append({"playerId": pk.drop.id, "type": "DROP", "fromTeamId": self.team_id})
        if pk.waiver:
            return self._payload("WAIVER", items, bidAmount=bid)
        return self._payload("FREEAGENT", items)

    def submit(self, payload: dict) -> dict:
        r = requests.post(
            WRITE_URL.format(year=self.year, league_id=self.league_id),
            json=payload,
            cookies={"espn_s2": self.s2, "SWID": self.swid},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"ESPN rejected the transaction ({r.status_code}): {r.text[:300]}")
        return r.json() if r.content else {}


def player_value(pl, vcfg: dict) -> float:
    season = float(pl.avg_points or 0)
    proj = float(pl.projected_avg_points or 0)
    src = vcfg.get("source", "blend")
    if src == "season":
        v = season or proj
    elif src == "projected":
        v = proj or season
    else:
        v = (season + proj) / 2 if season and proj else season or proj
    if v:
        return round(v, 2)
    # Category leagues have no fantasy points; use ESPN's default points formula on averages.
    try:
        a = pl.nine_cat_averages
    except Exception:
        return 0.0
    w = {"PTS": 1, "REB": 1, "AST": 2, "STL": 4, "BLK": 4, "TO": -2, "3PM": 1}
    return round(sum(a.get(k, 0) * x for k, x in w.items()), 2)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
SLOT_NAME = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F", 7: "SG/SF",
             8: "G/F", 9: "PF/C", 10: "F/C", 11: "UTIL", 12: "Bench", 13: "IR"}


def describe_move(p: P, f: int, t: int) -> str:
    tag = f" ({p.status})" if p.status not in ("ACTIVE", "") else ""
    return f"{p.name}{tag}: {SLOT_NAME.get(f, f)} -> {SLOT_NAME.get(t, t)}"


def notify(cfg: dict, title: str, lines: list[str], urgent: bool = False) -> None:
    topic = os.environ.get("NTFY_TOPIC") or cfg["notify"]["ntfy_topic"]
    print(f"\n== {title} ==\n" + "\n".join(lines))
    if not topic:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data="\n".join(lines).encode(),
            headers={"Title": title, "Tags": "basketball", "Priority": "high" if urgent else "default"},
            timeout=10,
        )
    except requests.RequestException as exc:
        print(f"(notification failed: {exc})")


def run(cfg: dict, live: bool) -> None:
    mode = "LIVE" if live else "DRY RUN"
    espn = ESPN(cfg)
    counts = espn.slot_counts()
    now = int(time.time() * 1000)
    roster = espn.roster()
    report: list[str] = []
    urgent = False

    plan = plan_lineup(roster, counts, now, cfg["lineup"])

    wcfg = cfg["waivers"]
    if wcfg["enabled"]:
        used = espn.adds_last_7_days()
        if used >= wcfg["max_adds_per_week"]:
            print(f"Add limit reached ({used}/{wcfg['max_adds_per_week']} in the last 7 days).")
        else:
            missing = [n for n in wcfg["droppable"] if _norm(n) not in {_norm(p.name) for p in roster}]
            if missing:
                print(f"Note: droppable names not on your roster: {', '.join(missing)}")
            pick = plan_pickup(roster, espn.free_agent_pool(wcfg["pool_size"]), counts, now, cfg, plan)
            if pick:
                verb = "Waiver claim" if pick.waiver else "Added"
                line = f"{verb} {pick.add.name}" + (f", dropped {pick.drop.name}" if pick.drop else "")
                payload = espn.pickup_payload(pick, int(wcfg["faab_bid"]))
                if live:
                    espn.submit(payload)
                    time.sleep(2)
                    espn.load()
                    roster = espn.roster()
                else:
                    print(json.dumps(payload, indent=2))
                    roster = [p for p in roster if not pick.drop or p.id != pick.drop.id]
                    if not pick.waiver:
                        roster.append(replace(pick.add, slot=BENCH))
                report.append(f"{line} ({pick.reason})")
                plan = plan_lineup(roster, counts, now, cfg["lineup"])

    if cfg["lineup"]["enabled"] and plan.moves:
        payload = espn.lineup_payload(plan.moves)
        if live:
            espn.submit(payload)
        else:
            print(json.dumps(payload, indent=2))
        report += [describe_move(p, f, t) for p, f, t in plan.moves]
        urgent = any(p.status in cfg["lineup"]["bench_statuses"] for p, _, _ in plan.moves)

    for p in plan.too_late:
        report.append(f"{p.name} is {p.status} but his game already locked")
        urgent = True
    if plan.empty and wcfg["enabled"]:
        report.append(f"{len(plan.empty)} starting slot(s) still empty tonight")

    if report:
        notify(cfg, f"Fantasy autopilot ({mode})", report, urgent)
    elif cfg["notify"]["notify_on_no_changes"]:
        notify(cfg, f"Fantasy autopilot ({mode})", ["No changes needed."])
    else:
        print("No changes needed.")


def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        user = tomllib.load(f)
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    for section, values in user.items():
        cfg.setdefault(section, {}).update(values)
    for key in ("league_id", "team_id", "year"):
        if key not in cfg.get("espn", {}):
            raise SystemExit(f"config: [espn] {key} is required")
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.toml", type=Path)
    ap.add_argument("--live", action="store_true", help="actually submit moves (default is a dry run)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    live = args.live or os.environ.get("AUTOPILOT_LIVE") == "1"
    try:
        run(cfg, live)
    except SystemExit:
        raise
    except Exception as exc:
        hint = ""
        if "Access" in type(exc).__name__ or "401" in str(exc):
            hint = " Your ESPN cookies have probably expired; refresh ESPN_S2 and ESPN_SWID."
        notify(cfg, "Fantasy autopilot error", [f"{type(exc).__name__}: {exc}.{hint}"], urgent=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
