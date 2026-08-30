"""
The bridge: takes everything collected in football.db (teams + players
from the pipeline) and writes it into your HTML game file's embedded
DEFAULT_DATA.teams / DEFAULT_DATA.players arrays — same format the game
already expects (see emi_football schema).

This does NOT touch career mode, managers, lineups, or anything else in
the game — it only replaces the teams/players data blocks, and only for
teams that exist in both the database and your league config (so your
game's existing team roster/leagues stay intact unless you've added the
league via the pipeline).

Player ratings: the pipeline deliberately doesn't invent an arbitrary
0-100 rating (per your instruction that ratings are your call, not
scraped data). Every exported player defaults to rating 65 unless you've
set one in player_ratings — set them however you like:

    INSERT INTO player_ratings (player_id, as_of_date, rating)
    VALUES ('arsenal_declan_rice', '2026-08-29', 88);

Usage:
    python export/export_to_game.py --db football.db --html emi_football_v16_no_career.html \
        --out emi_football_v17.html
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3

DEFAULT_PLAYER_RATING = 65
DEFAULT_FORMATION = "4-2-3-1"

# reasonable defaults per position bucket for jersey formation slotting —
# the game just needs SOME position string, this maps API-Football's
# broader categories to the game's finer-grained ones where we can't tell exactly.
POSITION_MAP = {
    "Goalkeeper": "GK", "GK": "GK",
    "Defender": "CB", "CB": "CB", "LB": "LB", "RB": "RB",
    "Midfielder": "CM", "CM": "CM", "CDM": "CDM", "CAM": "CAM", "LM": "LM", "RM": "RM",
    "Attacker": "ST", "ST": "ST", "LW": "LW", "RW": "RW",
}


def positions_for(pos: str) -> list[str]:
    m = {
        "GK": ["GK"], "CB": ["CB"], "LB": ["LB"], "RB": ["RB"],
        "CDM": ["CDM", "CM"], "CM": ["CM", "CDM"], "CAM": ["CAM", "CM"],
        "LM": ["LM"], "RM": ["RM"], "LW": ["LW", "LM"], "RW": ["RW", "RM"], "ST": ["ST"],
    }
    return m.get(pos, [pos])


def get_player_rating(conn, player_id: str) -> int:
    row = conn.execute(
        """SELECT rating FROM player_ratings WHERE player_id=?
           ORDER BY as_of_date DESC LIMIT 1""", (player_id,)
    ).fetchone()
    return row[0] if row else DEFAULT_PLAYER_RATING


def get_team_rating_override(conn, team_id: str) -> tuple[float, float]:
    row = conn.execute(
        """SELECT attack_adj, defense_adj FROM user_ratings WHERE team_id=?
           ORDER BY as_of_date DESC LIMIT 1""", (team_id,)
    ).fetchone()
    return (row[0], row[1]) if row else (0.0, 0.0)


def build_teams_and_players(conn, league_ids: list[str] | None = None):
    teams_out, players_out = [], []

    team_q = "SELECT team_id, name, stadium, city, logo_url FROM teams"
    params = []
    if league_ids:
        # only teams that appear in matches for these leagues
        placeholders = ",".join("?" * len(league_ids))
        team_q = f"""SELECT DISTINCT t.team_id, t.name, t.stadium, t.city, t.logo_url FROM teams t
                     JOIN matches m ON (m.home_team_id=t.team_id OR m.away_team_id=t.team_id)
                     WHERE m.league_id IN ({placeholders})"""
        params = league_ids

    for team_id, name, stadium, city, logo in conn.execute(team_q, params).fetchall():
        attack_adj, defense_adj = get_team_rating_override(conn, team_id)
        base_rating = 70 + round((attack_adj - defense_adj) * 20)  # crude but transparent scaling
        base_rating = max(50, min(95, base_rating))

        teams_out.append({
            "id": team_id, "name": name or team_id,
            "stadium": stadium or "", "city": city or "",
            "logo": logo or "", "rating": base_rating,
            "formation": DEFAULT_FORMATION, "transferBudget": 20,
            "computedRating": base_rating,
        })

        squad = conn.execute(
            """SELECT p.player_id, p.name, p.nationality, p.position, h.squad_number
               FROM players p JOIN player_team_history h ON h.player_id = p.player_id
               WHERE h.team_id=? AND h.is_current=1""",
            (team_id,),
        ).fetchall()

        for i, (pid, pname, nat, pos, number) in enumerate(squad, start=1):
            mapped_pos = POSITION_MAP.get(pos, "CM")
            players_out.append({
                "id": pid, "team": team_id, "name": pname,
                "age": 25,  # birth_date not always available from source; adjust per-player if you have it
                "nationality": nat or "Unknown",
                "position": mapped_pos, "positions": positions_for(mapped_pos),
                "number": number or i,
                "rating": get_player_rating(conn, pid),
                "injured": False,
            })

    return teams_out, players_out


def js_obj(d: dict, key_order: list[str]) -> str:
    parts = []
    for k in key_order:
        v = d[k]
        if isinstance(v, (str, list)):
            v_json = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            v_json = "true" if v else "false"
        else:
            v_json = json.dumps(v)
        parts.append(f'"{k}":{v_json}')
    return "{" + ",".join(parts) + "}"


def parse_existing_array(html: str, key: str, next_key: str) -> list[dict]:
    """Pull the existing teams/players array out of the game file as real JSON."""
    pattern = re.compile(rf"{key}:\s*(\[.*?\])(?=,\s*\n\s*{next_key}:)", re.S)
    m = pattern.search(html)
    if not m:
        raise RuntimeError(f"Could not find '{key}: [...]' block in the HTML — is this the right file?")
    return json.loads(m.group(1))


def merge_by_id(existing: list[dict], new: list[dict], id_key: str = "id") -> list[dict]:
    """
    New/DB-sourced entries win for any id they cover; everything else in
    the existing game file is left exactly as it was. This is what makes
    it safe to run mid-backfill — teams/players not yet pulled just stay
    as they already were in the game.
    """
    merged = {e[id_key]: e for e in existing}
    added, updated = 0, 0
    for n in new:
        if n[id_key] in merged:
            updated += 1
        else:
            added += 1
        merged[n[id_key]] = n
    print(f"  {added} new, {updated} updated, {len(existing) - updated} left untouched from the existing game file")
    return list(merged.values())


def patch_html(html: str, teams: list[dict], players: list[dict]) -> str:
    team_key_order = ["id", "name", "stadium", "city", "logo", "rating", "formation",
                       "transferBudget", "computedRating"]
    player_key_order = ["id", "team", "name", "age", "nationality", "position",
                         "positions", "number", "rating", "injured"]

    print("Merging teams:")
    existing_teams = parse_existing_array(html, "teams", "players")
    merged_teams = merge_by_id(existing_teams, teams)

    print("Merging players:")
    existing_players = parse_existing_array(html, "players", "managers")
    merged_players = merge_by_id(existing_players, players)

    teams_js = ",".join(js_obj(t, team_key_order) for t in merged_teams)
    players_js = ",".join(js_obj(p, player_key_order) for p in merged_players)

    teams_pattern = re.compile(r"teams:\s*\[.*?\](?=,\s*\n\s*players:)", re.S)
    html = teams_pattern.sub(f"teams: [{teams_js}]", html, count=1)

    players_pattern = re.compile(r"players:\s*\[.*?\](?=,\s*\n\s*managers:)", re.S)
    html = players_pattern.sub(f"players: [{players_js}]", html, count=1)

    return html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="football.db")
    ap.add_argument("--html", required=True, help="the existing game HTML file to update")
    ap.add_argument("--out", required=True, help="where to write the updated HTML")
    ap.add_argument("--leagues", nargs="*", default=None,
                     help="only include these league_ids; omit for ALL teams in the DB "
                          "(warning: this REPLACES the game's entire team/player list)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    teams, players = build_teams_and_players(conn, args.leagues)
    print(f"Exporting {len(teams)} teams, {len(players)} players.")

    with open(args.html, encoding="utf-8") as f:
        html = f.read()

    patched = patch_html(html, teams, players)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(patched)

    print(f"Written to {args.out}")


if __name__ == "__main__":
    main()
