"""Cut probabilities for this week of the guillotine league.

How it works
1. Calibrate: look at every starter in the weeks already played and measure,
   per position, how actual points compared with ESPN's projection
   (average bias, and how widely results scatter around the projection).
2. Simulate: play out the rest of this week thousands of times. Points already
   scored are locked in; points still to come are drawn at random around the
   projection using the scatter measured in step 1.
3. Count: a team's cut probability is the share of simulations in which it
   finished with the lowest score.

Results are printed and also saved to cut_probabilities.json for the website.
"""

import functools
import json
import math
import random
import sys
from datetime import datetime
from pathlib import Path

from league_report import BENCH_SLOTS, SLOT_NAMES, connect

SIMULATIONS = 20000
GAME_HOURS = 3.25  # rough real-time length of an NFL game
MIN_SAMPLES = 30   # need this many past player-games to trust a position's calibration
MIN_SWAP_GAIN = 1.0  # a bench player must project this many points better to be assumed to start

HERE = Path(__file__).resolve().parent
CALIBRATION_FILE = HERE / "calibration.json"
OUTPUT_FILE = HERE / "cut_probabilities.json"
SITE_DATA_FILE = HERE / "site" / "data.js"  # the web page loads this

# Used only when a position has too little history. bias = actual / projected,
# spread = typical miss as a fraction of the projection.
FALLBACK = {"bias": 0.95, "spread": 0.60}
# D/ST and K can score negative, so they are simulated with a bell curve
# instead of the skewed curve used for everyone else.
BELL_CURVE_POSITIONS = {"D/ST", "K"}


def lineups(league, week):
    """Yield (team, live_score, espn_projection, lineup) for each team that week."""
    for box in league.box_scores(week):
        for team, score, projected, lineup in (
            (box.home_team, box.home_score, box.home_projected, box.home_lineup),
            (box.away_team, box.away_score, box.away_projected, box.away_lineup),
        ):
            if team is None or isinstance(team, int) or not lineup:
                continue
            yield team, score, projected, lineup


def calibrate(league):
    """Measure projection bias and spread per position from completed weeks."""
    through_week = league.current_week - 1
    if CALIBRATION_FILE.exists():
        cached = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
        if cached.get("league_id") == league.league_id and cached.get("through_week") == through_week:
            return cached["positions"]

    samples = {}  # position -> list of (projected, actual)
    for week in range(1, through_week + 1):
        for _, _, _, lineup in lineups(league, week):
            for p in lineup:
                if p.slot_position in BENCH_SLOTS or p.projected_points <= 0:
                    continue
                samples.setdefault(str(p.position), []).append((p.projected_points, p.points))

    positions = fit_calibration(samples)
    CALIBRATION_FILE.write_text(
        json.dumps({"league_id": league.league_id, "through_week": through_week, "positions": positions}, indent=2),
        encoding="utf-8",
    )
    return positions


def fit_calibration(samples):
    """samples: position -> list of (projected, actual). Returns bias and spread per position."""
    positions = {}
    for position, pairs in samples.items():
        if len(pairs) < MIN_SAMPLES:
            continue
        total_projected = sum(proj for proj, _ in pairs)
        bias = sum(actual for _, actual in pairs) / total_projected
        squared_miss = sum((actual - bias * proj) ** 2 for proj, actual in pairs)
        positions[position] = {
            "samples": len(pairs),
            "bias": round(bias, 4),
            # relative spread: typical miss as a fraction of the adjusted projection
            "spread": round(math.sqrt(squared_miss / sum((bias * proj) ** 2 for proj, _ in pairs)), 4),
            # absolute spread in points (used for D/ST and K)
            "spread_points": round(math.sqrt(squared_miss / len(pairs)), 4),
        }
    return positions


def fraction_remaining(player, now):
    """1.0 = game not started, 0.0 = game over (or no game this week)."""
    if player.on_bye_week or not hasattr(player, "game_date"):
        return 0.0
    hours_elapsed = (now - player.game_date).total_seconds() / 3600
    return min(1.0, max(0.0, 1 - hours_elapsed / GAME_HOURS))


def player_row(player, now, slot=None, assumed=None):
    """Plain-data description of one player, for the website.

    slot: the slot the model assumes he ends up in (defaults to where he is now).
    assumed: "in" if the model moved him into the lineup, "out" if it benched him.
    """
    slot = slot or player.slot_position
    remaining = fraction_remaining(player, now)
    if player.on_bye_week:
        status = "bye"
    elif remaining == 0:
        status = "final"
    elif player.projected_points <= 0:
        status = "out"
    elif remaining < 1:
        status = "playing"
    else:
        status = "upcoming"
    return {
        "name": player.name,
        "slot": SLOT_NAMES.get(slot, slot),
        "position": str(player.position),
        "nfl_team": str(player.proTeam),
        "points": round(player.points, 2),
        "projected": round(player.projected_points, 2),
        "status": status,
        "assumed": assumed,
    }


def team_rows(entry, now):
    """Starters and bench for the website, reflecting the assumed lineup."""
    was_starter = {id(p) for p in entry["lineup"] if p.slot_position not in BENCH_SLOTS}
    starting = {id(p) for _, p in entry["assumed"]}
    starters = [
        player_row(p, now, slot, None if id(p) in was_starter else "in")
        for slot, p in entry["assumed"]
    ]
    bench = [
        player_row(p, now, "BE" if id(p) in was_starter else None, "out" if id(p) in was_starter else None)
        for p in entry["lineup"]
        if id(p) not in starting
    ]
    return starters, bench


def build_sampler(player, calibration, now):
    """Return (points_locked_in, function that draws the points still to come)."""
    remaining = fraction_remaining(player, now)
    if remaining == 0 or player.projected_points <= 0:
        return player.points, None

    cal = calibration.get(str(player.position), FALLBACK)
    full_game_mean = player.projected_points * cal["bias"]

    if str(player.position) in BELL_CURVE_POSITIONS:
        mean = full_game_mean * remaining
        sd = cal.get("spread_points", FALLBACK["spread"] * full_game_mean) * math.sqrt(remaining)
        return player.points, lambda: random.gauss(mean, sd)

    # Gamma distribution: never negative, long right tail (boom weeks), which is
    # how skill-position scoring behaves. Mean and variance both shrink in
    # proportion to how much of the game is left.
    variance_ratio = cal["spread"] ** 2
    shape = remaining / variance_ratio
    scale = variance_ratio * full_game_mean
    return player.points, lambda: random.gammavariate(shape, scale)


def game_started(player, now):
    """ESPN locks a player into his lineup slot once his game kicks off."""
    return not player.on_bye_week and hasattr(player, "game_date") and now >= player.game_date


def expected_points(player, calibration):
    """Bias-adjusted projection for a full game (0 if no game or ruled out)."""
    if player.on_bye_week or not hasattr(player, "game_date") or player.projected_points <= 0:
        return 0.0
    return player.projected_points * calibration.get(str(player.position), FALLBACK)["bias"]


def best_lineup(lineup, calibration, now, ignore_locks=False):
    """The lineup we assume the manager ends up with.

    Players whose games have started are locked where they are. Every other
    starting slot goes to whichever unlocked players (starters or bench) give
    the highest total projection, respecting which slots each player may fill.
    A bench player only displaces a starter if he projects more than
    MIN_SWAP_GAIN points better, so near-ties stay as the manager set them.

    Returns a list of (slot, player) for the assumed starters.
    """
    starters = [p for p in lineup if p.slot_position not in BENCH_SLOTS]
    bench = [p for p in lineup if p.slot_position == "BE"]  # IR players can't be started

    def locked(p):
        return not ignore_locks and game_started(p, now)

    assumed = [(p.slot_position, p) for p in starters if locked(p)]
    open_slots = [p.slot_position for p in starters if not locked(p)]
    pool = [p for p in starters + bench if not locked(p)]
    current_slot = {id(p): p.slot_position for p in starters}

    def value(p, slot):
        worth = expected_points(p, calibration)
        if id(p) in current_slot:
            worth += MIN_SWAP_GAIN                                 # incumbents win near-ties
            worth += 0.001 if current_slot[id(p)] == slot else 0   # and prefer their own slot
        return worth

    @functools.lru_cache(maxsize=None)
    def solve(slot_index, used):
        """Best (total value, choices) for open_slots[slot_index:], given players already used."""
        if slot_index == len(open_slots):
            return 0.0, ()
        slot = open_slots[slot_index]
        best_total, best_rest = solve(slot_index + 1, used)  # option: leave the slot empty
        best_choice = None
        for i, p in enumerate(pool):
            if used >> i & 1 or slot not in p.eligibleSlots:
                continue
            total, rest = solve(slot_index + 1, used | 1 << i)
            total += value(p, slot)
            if total > best_total:
                best_total, best_rest, best_choice = total, rest, i
        return best_total, (best_choice,) + best_rest

    _, choices = solve(0, 0)
    assumed += [(slot, pool[i]) for slot, i in zip(open_slots, choices) if i is not None]
    return assumed


def simulate(teams, calibration, now):
    """teams: list of dicts with 'assumed' starters. Adds simulation results to each dict."""
    prepared = []
    for entry in teams:
        locked = 0.0
        samplers = []
        for _, p in entry["assumed"]:
            points, sampler = build_sampler(p, calibration, now)
            locked += points
            if sampler:
                samplers.append(sampler)
        entry["players_left"] = len(samplers)
        prepared.append((locked, samplers))

    cut_counts = [0] * len(teams)
    totals = [[] for _ in teams]
    for _ in range(SIMULATIONS):
        scores = [locked + sum(draw() for draw in samplers) for locked, samplers in prepared]
        for i, score in enumerate(scores):
            totals[i].append(score)
        cut_counts[scores.index(min(scores))] += 1

    for i, entry in enumerate(teams):
        results = sorted(totals[i])
        entry["cut_probability"] = cut_counts[i] / SIMULATIONS
        entry["sim_mean"] = sum(results) / SIMULATIONS
        entry["sim_low"] = results[int(SIMULATIONS * 0.10)]
        entry["sim_high"] = results[int(SIMULATIONS * 0.90)]


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    league, _ = connect()
    week = league.current_week
    now = datetime.now()

    calibration = calibrate(league)
    print(f"{league.settings.name} - week {week} cut probabilities  ({now:%a %b %d %I:%M %p})")

    print(f"\nCalibration from weeks 1-{week - 1} (actual points vs ESPN projection, starters only)")
    print(f"  {'Pos':<6}{'Games':>6}{'Actual/Proj':>13}{'Typical miss':>14}")
    for position, cal in sorted(calibration.items()):
        print(f"  {position:<6}{cal['samples']:>6}{cal['bias']:>13.2f}{cal['spread_points']:>10.1f} pts")

    teams = [
        {"team": team, "live": score, "espn_projected": projected, "lineup": lineup}
        for team, score, projected, lineup in lineups(league, week)
    ]
    if not teams:
        sys.exit("ESPN returned no lineups for this week.")

    for t in teams:
        t["assumed"] = best_lineup(t["lineup"], calibration, now)
        t["starters"], t["bench"] = team_rows(t, now)

    simulate(teams, calibration, now)
    teams.sort(key=lambda t: t["cut_probability"], reverse=True)

    print(f"\n{'Team':<30}{'Cut %':>7}{'Live':>8}{'ESPN proj':>11}{'Sim avg':>9}{'Likely range':>16}{'Left':>6}")
    for t in teams:
        likely = f"{t['sim_low']:.0f}-{t['sim_high']:.0f}"
        print(
            f"{t['team'].team_name[:29]:<30}{t['cut_probability'] * 100:>6.1f}%{t['live']:>8.2f}"
            f"{t['espn_projected']:>11.2f}{t['sim_mean']:>9.1f}{likely:>16}{t['players_left']:>6}"
        )
    print("\n  Likely range = the middle 80% of simulated final scores.")
    print("  Left = starters with points still to come.")
    print(f"  Based on {SIMULATIONS:,} simulations, assuming each manager fixes obvious lineup mistakes.")

    print("\nLineup changes the model assumed:")
    any_swaps = False
    for t in teams:
        moved_in = [p["name"] for p in t["starters"] if p["assumed"] == "in"]
        moved_out = [p["name"] for p in t["bench"] if p["assumed"] == "out"]
        if moved_in or moved_out:
            any_swaps = True
            print(f"  {t['team'].team_name}: start {', '.join(moved_in) or 'nobody'}; bench {', '.join(moved_out) or 'nobody'}")
    if not any_swaps:
        print("  None - every lineup is already at (or within a point of) its best.")

    output = json.dumps(
            {
                "league": league.settings.name,
                "season": league.year,
                "week": week,
                # Includes the UTC offset so browsers show the right local time
                # no matter which time zone the script ran in.
                "generated_at": now.astimezone().isoformat(timespec="seconds"),
                "simulations": SIMULATIONS,
                "teams": [
                    {
                        "team_id": t["team"].team_id,
                        "team_name": t["team"].team_name,
                        "cut_probability": round(t["cut_probability"], 4),
                        "live_score": t["live"],
                        "espn_projected": t["espn_projected"],
                        "simulated_mean": round(t["sim_mean"], 2),
                        "simulated_low": round(t["sim_low"], 2),
                        "simulated_high": round(t["sim_high"], 2),
                        "starters_remaining": t["players_left"],
                        "starters": t["starters"],
                        "bench": t["bench"],
                    }
                    for t in teams
                ],
            },
            indent=2,
    )
    OUTPUT_FILE.write_text(output, encoding="utf-8")
    SITE_DATA_FILE.parent.mkdir(exist_ok=True)
    # Write to a temporary file and swap it in, so the page never loads a half-written file.
    temp_file = SITE_DATA_FILE.with_suffix(".tmp")
    temp_file.write_text(f"window.CUT_DATA = {output};\n", encoding="utf-8")
    temp_file.replace(SITE_DATA_FILE)
    print(f"\nSaved {OUTPUT_FILE.name} and site/{SITE_DATA_FILE.name}")


if __name__ == "__main__":
    main()
