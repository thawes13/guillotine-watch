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

    CALIBRATION_FILE.write_text(
        json.dumps({"league_id": league.league_id, "through_week": through_week, "positions": positions}, indent=2),
        encoding="utf-8",
    )
    return positions


def fraction_remaining(player, now):
    """1.0 = game not started, 0.0 = game over (or no game this week)."""
    if player.on_bye_week or not hasattr(player, "game_date"):
        return 0.0
    hours_elapsed = (now - player.game_date).total_seconds() / 3600
    return min(1.0, max(0.0, 1 - hours_elapsed / GAME_HOURS))


def player_row(player, now):
    """Plain-data description of one player, for the website."""
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
        "slot": SLOT_NAMES.get(player.slot_position, player.slot_position),
        "position": str(player.position),
        "nfl_team": str(player.proTeam),
        "points": round(player.points, 2),
        "projected": round(player.projected_points, 2),
        "status": status,
    }


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


def simulate(teams, calibration, now):
    """teams: list of dicts with 'lineup'. Adds simulation results to each dict."""
    prepared = []
    for entry in teams:
        locked = 0.0
        samplers = []
        for p in entry["lineup"]:
            if p.slot_position in BENCH_SLOTS:
                continue
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
    print(f"  Based on {SIMULATIONS:,} simulations of current starting lineups.")

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
                        "starters": [player_row(p, now) for p in t["lineup"] if p.slot_position not in BENCH_SLOTS],
                        "bench": [player_row(p, now) for p in t["lineup"] if p.slot_position in BENCH_SLOTS],
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
