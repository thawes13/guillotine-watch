"""Backtest: would the model's pre-week cut probabilities have been any good?

For each completed week it pretends no games have been played, simulates the
week from ESPN's projections for the lineups that were actually started, and
compares the result with what really happened.

To keep it honest, the calibration used for a given week is fitted on the
OTHER weeks only, so the model never sees the answers it is being graded on.
"""

import math
import sys
from datetime import datetime

import cut_odds
from league_report import BENCH_SLOTS, connect

SIMULATIONS = 5000
BEFORE_KICKOFF = datetime(2000, 1, 1)  # a time before every game, so nothing is "played" yet


def load_weeks(league):
    """week -> list of {team, actual, starters} for every completed week."""
    weeks = {}
    for week in range(1, league.current_week):
        weeks[week] = [
            {"team": team, "actual": score, "starters": [p for p in lineup if p.slot_position not in BENCH_SLOTS]}
            for team, score, _, lineup in cut_odds.lineups(league, week)
        ]
    return weeks


def calibration_without(weeks, held_out_week):
    samples = {}
    for week, entries in weeks.items():
        if week == held_out_week:
            continue
        for entry in entries:
            for p in entry["starters"]:
                if p.projected_points > 0:
                    samples.setdefault(str(p.position), []).append((p.projected_points, p.points))
    return cut_odds.fit_calibration(samples)


def simulate_week(entries, calibration):
    """Adds cut_probability and percentile (where the real score fell among simulated ones)."""
    team_samplers = []
    for entry in entries:
        samplers = []
        for p in entry["starters"]:
            _, sampler = cut_odds.build_sampler(p, calibration, BEFORE_KICKOFF)
            if sampler:
                samplers.append(sampler)
        team_samplers.append(samplers)

    cut_counts = [0] * len(entries)
    below_actual = [0] * len(entries)
    for _ in range(SIMULATIONS):
        scores = [sum(draw() for draw in samplers) for samplers in team_samplers]
        cut_counts[scores.index(min(scores))] += 1
        for i, score in enumerate(scores):
            if score < entries[i]["actual"]:
                below_actual[i] += 1

    for i, entry in enumerate(entries):
        entry["cut_probability"] = cut_counts[i] / SIMULATIONS
        entry["percentile"] = below_actual[i] / SIMULATIONS


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    league, _ = connect()
    weeks = load_weeks(league)
    print(f"Backtest of weeks 1-{max(weeks)}: pre-week predictions vs what happened\n")

    print(f"{'Wk':>2} {'Teams':>5}  {'Team actually cut':<28}{'Model gave':>11}{'Random':>8}{'Risk rank':>11}   Model's top pick")
    all_entries = []
    model_log_loss = baseline_log_loss = 0.0
    model_brier = baseline_brier = 0.0
    ranks = []
    for week, entries in weeks.items():
        simulate_week(entries, calibration_without(weeks, week))
        n = len(entries)
        cut = min(entries, key=lambda e: e["actual"])
        by_risk = sorted(entries, key=lambda e: e["cut_probability"], reverse=True)
        rank = by_risk.index(cut) + 1
        ranks.append((rank, n))

        p_cut = max(cut["cut_probability"], 0.5 / SIMULATIONS)  # avoid log(0)
        model_log_loss += -math.log(p_cut)
        baseline_log_loss += -math.log(1 / n)
        for e in entries:
            happened = 1.0 if e is cut else 0.0
            model_brier += (e["cut_probability"] - happened) ** 2
            baseline_brier += (1 / n - happened) ** 2
        all_entries.extend((e, e is cut) for e in entries)

        top = by_risk[0]
        print(
            f"{week:>2} {n:>5}  {cut['team'].team_name[:27]:<28}{cut['cut_probability'] * 100:>10.1f}%"
            f"{100 / n:>7.1f}%{f'{rank} of {n}':>11}   {top['team'].team_name[:22]} ({top['cut_probability'] * 100:.0f}%)"
        )

    count = len(weeks)
    print("\nDid it beat guessing at random?  (lower is better)")
    print(f"  {'':<22}{'Model':>8}{'Random':>8}")
    print(f"  {'Log loss per week':<22}{model_log_loss / count:>8.3f}{baseline_log_loss / count:>8.3f}")
    print(f"  {'Brier score per week':<22}{model_brier / count:>8.3f}{baseline_brier / count:>8.3f}")
    top3 = sum(1 for rank, _ in ranks if rank <= 3)
    print(f"  Cut team was in the model's top 3 most at-risk in {top3} of {count} weeks.")

    print("\nAre the percentages honest?  (all team-weeks grouped by predicted chance)")
    print(f"  {'Predicted':<12}{'Team-weeks':>11}{'Avg predicted':>15}{'Expected cuts':>15}{'Actual cuts':>13}")
    for low, high in ((0, 0.03), (0.03, 0.08), (0.08, 0.15), (0.15, 1.01)):
        group = [(e, was_cut) for e, was_cut in all_entries if low <= e["cut_probability"] < high]
        if not group:
            continue
        expected = sum(e["cut_probability"] for e, _ in group)
        actual = sum(1 for _, was_cut in group if was_cut)
        label = f"{low * 100:.0f}-{min(high, 1) * 100:.0f}%"
        print(f"  {label:<12}{len(group):>11}{expected / len(group) * 100:>14.1f}%{expected:>15.1f}{actual:>13}")

    percentiles = [e["percentile"] for e, _ in all_entries]
    total = len(percentiles)
    inside_80 = sum(1 for p in percentiles if 0.10 <= p <= 0.90)
    inside_50 = sum(1 for p in percentiles if 0.25 <= p <= 0.75)
    below_10 = sum(1 for p in percentiles if p < 0.10)
    above_90 = sum(1 for p in percentiles if p > 0.90)
    print(f"\nAre the score ranges the right width?  ({total} team-weeks)")
    print(f"  Real score inside the 80% 'likely range': {inside_80 / total * 100:.0f}%   (ideal: 80%)")
    print(f"  Real score inside the middle 50%:         {inside_50 / total * 100:.0f}%   (ideal: 50%)")
    print(f"  Real score below the range: {below_10 / total * 100:.0f}%, above it: {above_90 / total * 100:.0f}%   (ideal: 10% each)")
    print(f"  Average percentile of real scores: {sum(percentiles) / total * 100:.0f}   (ideal: 50)")

    eliminated = {t.team_name for t in league.teams if not t.roster}
    lowest_scorers = {min(entries, key=lambda e: e["actual"])["team"].team_name for entries in weeks.values()}
    print(f"\nSanity check - weekly lowest scorers match the teams ESPN shows as eliminated: {lowest_scorers == eliminated}")


if __name__ == "__main__":
    main()
