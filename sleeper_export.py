#!/usr/bin/env python3

import argparse
import csv
import json
import os
import sys
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_BASE = "https://api.sleeper.app/v1"


def http_get(path: str, timeout: int = 20) -> dict:
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    req = Request(url, headers={"User-Agent": "sleeper-export-cli/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if not data:
                return {}
            return json.loads(data.decode("utf-8"))
    except HTTPError as e:
        raise SystemExit(f"HTTP error {e.code} for {url}")
    except URLError as e:
        raise SystemExit(f"Network error for {url}: {e}")


def resolve_user_id(username: Optional[str], user_id: Optional[str]) -> str:
    if user_id:
        return user_id
    if not username:
        raise SystemExit("Error: either --username or --user-id must be provided")
    data = http_get(f"/user/{username}")
    if not data or "user_id" not in data:
        raise SystemExit(f"Error: could not resolve user id for username '{username}'")
    return data["user_id"]


def get_active_league_season() -> str:
    data = http_get("/state/nfl")
    # Prefer 'league_season' when available; fall back to 'season'
    return str(data.get("league_season") or data.get("season"))


def get_user_leagues(user_id: str, season: str) -> List[dict]:
    return http_get(f"/user/{user_id}/leagues/nfl/{season}") or []


def get_league(league_id: str) -> dict:
    return http_get(f"/league/{league_id}") or {}


def get_league_users(league_id: str) -> List[dict]:
    return http_get(f"/league/{league_id}/users") or []


def get_league_rosters(league_id: str) -> List[dict]:
    return http_get(f"/league/{league_id}/rosters") or []


def get_league_drafts(league_id: str) -> List[dict]:
    # Sorted most recent first per docs
    return http_get(f"/league/{league_id}/drafts") or []


def get_draft(draft_id: str) -> dict:
    return http_get(f"/draft/{draft_id}") or {}


def get_players_index() -> Dict[str, dict]:
    # Large payload (~5MB)
    return http_get("/players/nfl") or {}


def map_player_id(player: dict) -> str:
    # Build a compact human-readable label
    first = player.get("first_name") or ""
    last = player.get("last_name") or ""
    full = (first + " " + last).strip()
    if not full:
        full = player.get("full_name") or player.get("last_name") or player.get("first_name") or ""
    pos = player.get("position") or ""
    team = player.get("team") or ""
    if full and (pos or team):
        return f"{full} ({pos}-{team})".strip(" -()")
    return full or player.get("player_id") or ""


def partition_starters_bench(starters: List[str], all_players: List[str]) -> Tuple[List[str], List[str]]:
    starters_set = set(starters or [])
    bench = [p for p in all_players if p not in starters_set]
    return list(starters or []), bench


def find_my_roster(rosters: List[dict], user_id: str) -> Optional[dict]:
    for r in rosters:
        if str(r.get("owner_id")) == str(user_id):
            return r
    return None


def resolve_draft_slot_for_user(draft_obj: dict, my_user_id: str, my_roster_id: Optional[int]) -> Optional[int]:
    if not draft_obj:
        return None
    draft_order = draft_obj.get("draft_order") or {}
    slot_to_roster = draft_obj.get("slot_to_roster_id") or {}

    # Try user_id -> slot first
    if my_user_id and str(my_user_id) in draft_order:
        try:
            return int(draft_order[str(my_user_id)])
        except Exception:
            pass

    # Fallback: find slot that maps to my roster_id
    if my_roster_id is not None:
        for slot_str, rid in (slot_to_roster or {}).items():
            try:
                if int(rid) == int(my_roster_id):
                    return int(slot_str)
            except Exception:
                continue
    return None


def resolve_draft_slot_for_roster(draft_obj: dict, roster_id: Optional[int]) -> Optional[int]:
    if not draft_obj or roster_id is None:
        return None
    slot_to_roster = draft_obj.get("slot_to_roster_id") or {}
    for slot_str, rid in slot_to_roster.items():
        try:
            if int(rid) == int(roster_id):
                return int(slot_str)
        except Exception:
            continue
    return None


def build_export_rows(
    user_id: str,
    season: str,
    include_names: bool,
) -> List[Dict[str, str]]:
    leagues = get_user_leagues(user_id, season)

    # Optional players index for names
    players_index: Dict[str, dict] = {}
    if include_names:
        players_index = get_players_index()

    rows: List[Dict[str, str]] = []

    for league in leagues:
        league_id = league.get("league_id")
        league_name = league.get("name") or ""
        league_season = str(league.get("season") or season)

        # Fetch league users and rosters
        users = get_league_users(league_id)
        user_map = {u.get("user_id"): u for u in users}
        rosters = get_league_rosters(league_id)

        my_roster = find_my_roster(rosters, user_id)
        if not my_roster:
            # In some rare cases, owner_id can be None or a co-owner situation; skip if not found
            continue

        my_roster_id = my_roster.get("roster_id")
        starters_ids = my_roster.get("starters") or []
        player_ids = my_roster.get("players") or []
        starters_list, bench_list = partition_starters_bench(starters_ids, player_ids)

        # Determine my display name and team name
        me_user = user_map.get(str(user_id))
        owner_display = (me_user or {}).get("display_name") or (me_user or {}).get("username") or ""
        team_name = ((me_user or {}).get("metadata") or {}).get("team_name") or ""

        # Resolve draft slot
        draft_id = league.get("draft_id")
        draft_slot: Optional[int] = None
        if draft_id:
            draft_obj = get_draft(draft_id)
            draft_slot = resolve_draft_slot_for_user(draft_obj, str(user_id), my_roster_id)
        else:
            # fallback: try league drafts endpoint and pick most recent
            drafts = get_league_drafts(league_id)
            if drafts:
                draft_obj = get_draft(drafts[0].get("draft_id"))
                draft_slot = resolve_draft_slot_for_user(draft_obj, str(user_id), my_roster_id)

        def map_ids(ids: List[str]) -> str:
            if not include_names:
                return ";".join(ids)
            out: List[str] = []
            for pid in ids:
                pdata = players_index.get(str(pid))
                out.append(map_player_id(pdata) if pdata else str(pid))
            return ";".join(out)

        row = {
            "season": league_season,
            "league_name": league_name,
            "league_id": str(league_id),
            "team_name": team_name,
            "owner_display_name": owner_display,
            "roster_id": str(my_roster_id or ""),
            "draft_slot": str(draft_slot or ""),
            "starters": map_ids(starters_list),
            "bench": map_ids(bench_list),
            "all_players": map_ids(player_ids),
        }
        rows.append(row)

    return rows


def build_export_rows_for_league(
    league_id: str,
    include_names: bool,
) -> List[Dict[str, str]]:
    league = get_league(league_id)
    league_name = league.get("name") or ""
    league_season = str(league.get("season") or "")

    users = get_league_users(league_id)
    user_map = {u.get("user_id"): u for u in users}
    rosters = get_league_rosters(league_id)

    # Resolve draft
    draft_id = league.get("draft_id")
    draft_obj: dict = {}
    if draft_id:
        draft_obj = get_draft(draft_id)
    else:
        drafts = get_league_drafts(league_id)
        if drafts:
            draft_obj = get_draft(drafts[0].get("draft_id"))

    players_index: Dict[str, dict] = {}
    if include_names:
        players_index = get_players_index()

    def map_ids(ids: List[str]) -> str:
        if not include_names:
            return ";".join(ids or [])
        out: List[str] = []
        for pid in (ids or []):
            pdata = players_index.get(str(pid))
            out.append(map_player_id(pdata) if pdata else str(pid))
        return ";".join(out)

    rows: List[Dict[str, str]] = []
    for r in rosters:
        roster_id = r.get("roster_id")
        owner_uid = r.get("owner_id")
        starters_ids = r.get("starters") or []
        player_ids = r.get("players") or []
        starters_list, bench_list = partition_starters_bench(starters_ids, player_ids)

        user = user_map.get(str(owner_uid)) or {}
        owner_display = user.get("display_name") or user.get("username") or ""
        team_name = (user.get("metadata") or {}).get("team_name") or ""

        draft_slot = resolve_draft_slot_for_roster(draft_obj, roster_id)

        row = {
            "season": league_season,
            "league_name": league_name,
            "league_id": str(league_id),
            "team_name": team_name,
            "owner_display_name": owner_display,
            "roster_id": str(roster_id or ""),
            "draft_slot": str(draft_slot or ""),
            "starters": map_ids(starters_list),
            "bench": map_ids(bench_list),
            "all_players": map_ids(player_ids),
        }
        rows.append(row)

    return rows


def write_csv(rows: List[Dict[str, str]], out_path: Optional[str]) -> None:
    fieldnames = [
        "season",
        "league_name",
        "league_id",
        "team_name",
        "owner_display_name",
        "roster_id",
        "draft_slot",
        "starters",
        "bench",
        "all_players",
    ]

    if out_path:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Sleeper leagues roster and draft position to CSV",
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--username", help="Sleeper username to export for")
    g.add_argument("--user-id", help="Sleeper user_id to export for")
    g.add_argument("--league-id", help="Sleeper league_id to export (exports all rosters)")
    parser.add_argument("--season", help="Season (e.g. 2024). Defaults to active league season.")
    parser.add_argument("--names", action="store_true", help="Include player names (calls large /players endpoint)")
    parser.add_argument("--out", help="Output CSV path. If omitted, prints to stdout.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    if args.league_id:
        league_id = str(args.league_id).strip().strip("/")
        rows = build_export_rows_for_league(league_id=league_id, include_names=args.names)
        write_csv(rows, args.out)
        return

    user_id = resolve_user_id(args.username, args.user_id)
    season = args.season or get_active_league_season()

    rows = build_export_rows(user_id=user_id, season=season, include_names=args.names)

    write_csv(rows, args.out)


if __name__ == "__main__":
    main()