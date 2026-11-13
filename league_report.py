"""Guillotine league report: teams, this week's live scores, recent transactions.

Tries the league as public first. If ESPN says it's private, it uses the
ESPN_S2 and SWID cookies from the .env file next to this script.
The cookie values are never printed.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from espn_api.football import League
from espn_api.requests.espn_requests import ESPNAccessDenied, ESPNInvalidLeague

LEAGUE_ID = 626860
YEAR = 2026
TRANSACTION_COUNT = 25

HERE = Path(__file__).resolve().parent
BENCH_SLOTS = {"BE", "IR"}
SLOT_NAMES = {"RB/WR/TE": "FLEX"}  # shorter label so the columns line up


def connect():
    """Return (league, mode). Public first, then cookies from .env."""
    try:
        return League(league_id=LEAGUE_ID, year=YEAR), "public"
    except ESPNAccessDenied:
        pass

    load_dotenv(HERE / ".env")
    espn_s2 = (os.getenv("ESPN_S2") or "").strip()
    swid = (os.getenv("SWID") or "").strip()
    if not espn_s2 or not swid:
        sys.exit(
            "LOGIN NEEDED: this league is private and no cookies were found.\n"
            "Fill in ESPN_S2 and SWID in the .env file, then run this again."
        )
    try:
        return League(league_id=LEAGUE_ID, year=YEAR, espn_s2=espn_s2, swid=swid), "private (cookies)"
    except ESPNAccessDenied:
        sys.exit(
            "LOGIN FAILED: ESPN rejected the cookies in .env. They may be expired, "
            "mistyped, or belong to an account that isn't in this league."
        )


def header(text):
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def team_label(team):
    if team is None or team == "":
        return "(unknown team)"
    return getattr(team, "team_name", None) or str(team)


def player_label(player):
    return getattr(player, "name", None) or f"player #{player}"


def print_teams(league):
    header(f"TEAMS  ({len(league.teams)} total)")
    print(f"{'Team':<32}{'Roster':>7}{'Season pts':>12}{'FAAB spent':>12}")
    for team in sorted(league.teams, key=lambda t: t.team_name.lower()):
        spent = getattr(team, "acquisition_budget_spent", None)
        spent_text = f"${spent}" if spent is not None else "-"
        note = "   <- empty roster (eliminated?)" if not team.roster else ""
        print(f"{team.team_name[:31]:<32}{len(team.roster):>7}{team.points_for:>12.2f}{spent_text:>12}{note}")


def print_live_scores(league):
    week = league.current_week
    header(f"WEEK {week} LIVE SCORES")

    # A guillotine league has no real head-to-head, so flatten every
    # box score into one list of teams and rank them by score.
    rows = []
    for box in league.box_scores(week):
        for team, score, projected, lineup in (
            (box.home_team, box.home_score, box.home_projected, box.home_lineup),
            (box.away_team, box.away_score, box.away_projected, box.away_lineup),
        ):
            if team is None or isinstance(team, int) or not lineup:
                continue
            rows.append((team, score, projected, lineup))

    if not rows:
        print("ESPN returned no lineups for this week.")
        return

    rows.sort(key=lambda r: r[1], reverse=True)

    print(f"{'#':>2}  {'Team':<32}{'Live':>9}{'Projected':>11}")
    for rank, (team, score, projected, _) in enumerate(rows, 1):
        print(f"{rank:>2}  {team.team_name[:31]:<32}{score:>9.2f}{projected:>11.2f}")
    print("    (lowest score at the end of the week gets cut)")

    for rank, (team, score, projected, lineup) in enumerate(rows, 1):
        print(f"\n--- #{rank} {team.team_name}: {score:.2f} live, {projected:.2f} projected ---")
        print(f"  {'Slot':<6}{'Player':<26}{'Pos':<5}{'NFL':<5}{'Pts':>7}{'Proj':>8}  Status")
        starters = [p for p in lineup if p.slot_position not in BENCH_SLOTS]
        bench = [p for p in lineup if p.slot_position in BENCH_SLOTS]
        for p in starters + bench:
            if p.on_bye_week:
                status = "bye"
            elif p.game_played >= 100:
                status = "final"
            else:
                status = "not final"
            print(
                f"  {SLOT_NAMES.get(p.slot_position, p.slot_position):<6}{p.name[:25]:<26}{str(p.position):<5}{str(p.proTeam):<5}"
                f"{p.points:>7.2f}{p.projected_points:>8.2f}  {status}"
            )


def print_transactions(league):
    header(f"LAST {TRANSACTION_COUNT} TRANSACTIONS")
    activities = league.recent_activity(size=TRANSACTION_COUNT)
    if not activities:
        print("No transactions found.")
        return

    for activity in activities:
        when = datetime.fromtimestamp(activity.date / 1000).strftime("%a %b %d %I:%M %p")
        for team, action, player, bid in activity.actions:
            bid_text = f"   FAAB bid: ${bid}" if action == "WAIVER ADDED" else ""
            print(f"{when}  {team_label(team)[:26]:<27}{action:<15}{player_label(player)}{bid_text}")


def main():
    # Windows consoles can choke on accented player/team names without this.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        league, mode = connect()
    except ESPNInvalidLeague:
        sys.exit(f"ESPN says league {LEAGUE_ID} does not exist for the {YEAR} season.")

    print(f"Connected to \"{league.settings.name}\" ({YEAR}) as a {mode} league.")
    print(f"Current week: {league.current_week}")

    print_teams(league)
    print_live_scores(league)
    print_transactions(league)


if __name__ == "__main__":
    main()
