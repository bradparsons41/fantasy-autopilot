"""Offline tests for the decision logic (no ESPN access needed). Run: python -m pytest -q"""
import copy
from dataclasses import replace

from autopilot import BENCH, DEFAULTS, IR, P, plan_lineup, plan_pickup

PG, SG, SF, PF, C, G, F, UT = 0, 1, 2, 3, 4, 5, 6, 11
NOW = 1_000_000_000_000
LATER = NOW + 3 * 3_600_000      # tips off in 3 hours
STARTED = NOW - 600_000          # tipped off 10 minutes ago
COUNTS = {PG: 1, C: 1, UT: 1, BENCH: 2, IR: 1}  # roster limit 5


def p(id, name, slot, elig, value, status="ACTIVE", game=LATER, pool=""):
    return P(id, name, slot, frozenset(elig) | {UT, BENCH}, value, status, game, pool)


def cfg(**waivers):
    c = copy.deepcopy(DEFAULTS)
    c["waivers"].update(enabled=True, **waivers)
    return c


def moves(plan):
    return {m[0].name: (m[1], m[2]) for m in plan.moves}


def test_out_starter_benched_and_replaced():
    roster = [
        p(1, "Guard", PG, {PG}, 40, status="OUT"),
        p(2, "Big", C, {C}, 35),
        p(3, "Wing", UT, {SF}, 30),
        p(4, "BackupPG", BENCH, {PG}, 20),
    ]
    plan = plan_lineup(roster, COUNTS, NOW, DEFAULTS["lineup"])
    assert moves(plan) == {"Guard": (PG, BENCH), "BackupPG": (BENCH, PG)}
    assert plan.empty == []


def test_locked_players_are_never_touched():
    roster = [
        p(1, "LockedOut", PG, {PG}, 40, status="OUT", game=STARTED),
        p(2, "LockedBench", BENCH, {C}, 99, game=STARTED),
        p(3, "Big", C, {C}, 10),
        p(4, "Wing", UT, {SF}, 30),
    ]
    plan = plan_lineup(roster, COUNTS, NOW, DEFAULTS["lineup"])
    assert plan.moves == []
    assert [x.name for x in plan.too_late] == ["LockedOut"]


def test_lock_buffer():
    roster = [p(1, "Soon", BENCH, {PG}, 50, game=NOW + 2 * 60_000)]  # tips in 2 min, buffer 5
    plan = plan_lineup(roster, COUNTS, NOW, DEFAULTS["lineup"])
    assert plan.moves == []


def test_no_game_starter_swapped_for_player_with_game():
    roster = [
        p(1, "Idle", PG, {PG}, 45, game=None),
        p(2, "Big", C, {C}, 30),
        p(3, "Wing", UT, {SF}, 30),
        p(4, "Plays", BENCH, {PG}, 15),
    ]
    assert moves(plan_lineup(roster, COUNTS, NOW, DEFAULTS["lineup"])) == {
        "Idle": (PG, BENCH), "Plays": (BENCH, PG)}


def test_position_eligibility_uses_flex_reshuffle():
    # Best bench player is a C; the C slot frees up only if the current C slides to UTIL.
    roster = [
        p(1, "Guard", PG, {PG}, 30),
        p(2, "BigFlex", C, {C, PF}, 25),
        p(3, "Weak", UT, {SF}, 5),
        p(4, "StrongC", BENCH, {C}, 40),
    ]
    plan = plan_lineup(roster, COUNTS, NOW, DEFAULTS["lineup"])
    assert set(plan.starters) == {1, 2, 4}
    assert moves(plan)["Weak"] == (UT, BENCH)


def test_injuries_only_keeps_healthy_starters():
    lc = dict(DEFAULTS["lineup"], strategy="injuries_only")
    roster = [
        p(1, "Guard", PG, {PG}, 10),
        p(2, "Big", C, {C}, 30, status="OUT"),
        p(3, "Wing", UT, {SF}, 30),
        p(4, "BetterPG", BENCH, {PG}, 50),
        p(5, "BackupC", BENCH, {C}, 5),
    ]
    assert moves(plan_lineup(roster, COUNTS, NOW, lc)) == {"Big": (C, BENCH), "BackupC": (BENCH, C)}


def test_no_unnecessary_moves():
    roster = [p(1, "A", PG, {PG}, 30), p(2, "B", C, {C}, 30), p(3, "D", UT, {SF}, 30),
              p(4, "E", BENCH, {SF}, 10)]
    assert plan_lineup(roster, COUNTS, NOW, DEFAULTS["lineup"]).moves == []


def stream_roster():
    return [
        p(1, "Guard", PG, {PG}, 40, status="OUT"),
        p(2, "Big", C, {C}, 35),
        p(3, "Wing", UT, {SF}, 30),
        p(4, "Scrub", BENCH, {SF}, 8, game=None),
        p(5, "Keeper", BENCH, {PF}, 33, game=None),
    ]


def test_stream_fills_empty_slot_and_drops_only_droppable():
    c = cfg(droppable=["Scrub"])
    roster = stream_roster()
    base = plan_lineup(roster, COUNTS, NOW, c["lineup"])
    assert base.empty == [PG]
    pool = [p(10, "FA Center", None, {C}, 25, pool="FREEAGENT"),  # better, but can't fill PG
            p(11, "FA Guard", None, {PG}, 20, pool="FREEAGENT")]
    pick = plan_pickup(roster, pool, COUNTS, NOW, c, base)
    assert pick.add.name == "FA Guard" and pick.drop.name == "Scrub"
    sim = [x for x in roster if x.id != 4] + [replace(pick.add, slot=BENCH)]
    assert plan_lineup(sim, COUNTS, NOW, c["lineup"]).empty == []


def test_never_drops_unlisted_players():
    c = cfg(droppable=[])
    roster = stream_roster()
    base = plan_lineup(roster, COUNTS, NOW, c["lineup"])
    pool = [p(11, "FA Guard", None, {PG}, 99, pool="FREEAGENT")]
    assert plan_pickup(roster, pool, COUNTS, NOW, c, base) is None


def test_stream_skips_free_agents_without_a_game_or_on_waivers():
    c = cfg(droppable=["Scrub"])
    roster = stream_roster()
    base = plan_lineup(roster, COUNTS, NOW, c["lineup"])
    pool = [p(11, "NoGame", None, {PG}, 50, game=None, pool="FREEAGENT"),
            p(12, "OnWaivers", None, {PG}, 50, pool="WAIVERS"),
            p(13, "Hurt", None, {PG}, 50, status="OUT", pool="FREEAGENT")]
    assert plan_pickup(roster, pool, COUNTS, NOW, c, base) is None


def test_min_upgrade_protects_better_droppable_player():
    c = cfg(droppable=["Scrub"], min_upgrade=15)
    roster = stream_roster()
    base = plan_lineup(roster, COUNTS, NOW, c["lineup"])
    pool = [p(11, "FA Guard", None, {PG}, 20, pool="FREEAGENT")]  # 20 <= 8 + 15
    assert plan_pickup(roster, pool, COUNTS, NOW, c, base) is None


def test_no_stream_when_lineup_full():
    c = cfg(droppable=["Scrub"])
    roster = stream_roster()
    roster[0] = p(1, "Guard", PG, {PG}, 40)  # healthy now
    base = plan_lineup(roster, COUNTS, NOW, c["lineup"])
    pool = [p(11, "FA Guard", None, {PG}, 99, pool="FREEAGENT")]
    assert plan_pickup(roster, pool, COUNTS, NOW, c, base) is None


def test_upgrade_mode_and_waiver_claims():
    c = cfg(mode="upgrade", droppable=["Scrub"], min_upgrade=5, claim_waivers=True)
    roster = stream_roster()
    roster[0] = p(1, "Guard", PG, {PG}, 40)
    base = plan_lineup(roster, COUNTS, NOW, c["lineup"])
    pool = [p(11, "Waiver Stud", None, {SF}, 30, game=None, pool="WAIVERS")]
    pick = plan_pickup(roster, pool, COUNTS, NOW, c, base)
    assert pick.waiver and pick.drop.name == "Scrub"


def test_open_roster_spot_needs_no_drop():
    c = cfg(droppable=[])
    roster = stream_roster()[:4]  # 4 of 5 spots used
    base = plan_lineup(roster, COUNTS, NOW, c["lineup"])
    pool = [p(11, "FA Guard", None, {PG}, 12, pool="FREEAGENT")]
    pick = plan_pickup(roster, pool, COUNTS, NOW, c, base)
    assert pick.add.name == "FA Guard" and pick.drop is None
