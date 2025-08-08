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
    req = Request(url, headers={"User-Agent": "sleeper-export-cli/1.1"})
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


def get_draft_picks(draft_id: str) -> List[dict]:
    data = http_get(f"/draft/{draft_id}/picks")
    if isinstance(data, list):
        return data
    return []


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


def get_player_meta(player: Optional[dict]) -> Tuple[str, str, str]:
    if not player:
        return "", "", ""
    first = player.get("first_name") or ""
    last = player.get("last_name") or ""
    name = (first + " " + last).strip() or player.get("full_name") or ""
    pos = player.get("position") or ""
    team = player.get("team") or ""
    return name, pos, team


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


def build_player_rows_for_league(
    league_id: str,
    include_names: bool,
) -> List[Dict[str, str]]:
    league = get_league(league_id)
    league_name = league.get("name") or ""
    league_season = str(league.get("season") or "")

    users = get_league_users(league_id)
    user_map = {u.get("user_id"): u for u in users}
    rosters = get_league_rosters(league_id)

    # Draft and picks
    draft_id = league.get("draft_id")
    draft_obj: dict = {}
    picks: List[dict] = []
    if draft_id:
        draft_obj = get_draft(draft_id)
        picks = get_draft_picks(draft_id)
    else:
        drafts = get_league_drafts(league_id)
        if drafts:
            draft_id = drafts[0].get("draft_id")
            draft_obj = get_draft(draft_id)
            picks = get_draft_picks(draft_id)

    # Build player_id -> pick info map
    player_to_pick: Dict[str, dict] = {}
    for p in picks or []:
        pid = str(p.get("player_id")) if p.get("player_id") is not None else None
        if not pid:
            continue
        player_to_pick[pid] = {
            "round": p.get("round"),
            "pick_no": p.get("pick_no"),
            "draft_slot": p.get("draft_slot"),
            "picked_by_user_id": p.get("picked_by"),
            "picked_roster_id": p.get("roster_id"),
            "is_keeper": p.get("is_keeper"),
        }

    players_index: Dict[str, dict] = {}
    if include_names:
        players_index = get_players_index()

    rows: List[Dict[str, str]] = []

    for r in rosters:
        roster_id = r.get("roster_id")
        owner_uid = r.get("owner_id")
        user = user_map.get(str(owner_uid)) or {}
        owner_display = user.get("display_name") or user.get("username") or ""
        team_name = (user.get("metadata") or {}).get("team_name") or ""
        my_slot = resolve_draft_slot_for_roster(draft_obj, roster_id)

        for pid in (r.get("players") or []):
            pid_str = str(pid)
            pick_info = player_to_pick.get(pid_str) or {}
            drafted_round = pick_info.get("round")
            drafted_pick_no = pick_info.get("pick_no")
            drafted_by_roster_id = pick_info.get("picked_roster_id")

            pmeta = players_index.get(pid_str) if include_names else None
            name, pos, nfl_team = get_player_meta(pmeta)

            row = {
                "season": league_season,
                "league_name": league_name,
                "league_id": str(league_id),
                "roster_id": str(roster_id or ""),
                "draft_slot": str(my_slot or ""),
                "team_name": team_name,
                "owner_display_name": owner_display,
                "player_id": pid_str,
                "player_name": name,
                "position": pos,
                "nfl_team": nfl_team,
                "drafted_round": str(drafted_round or ""),
                "drafted_pick_no": str(drafted_pick_no or ""),
                "drafted_by_roster_id": str(drafted_by_roster_id or ""),
            }
            rows.append(row)

    return rows


def write_csv(rows: List[Dict[str, str]], out_path: Optional[str]) -> None:
    # Choose fieldnames dynamically; if player rows, include player columns
    preferred_player = [
        "season",
        "league_name",
        "league_id",
        "team_name",
        "owner_display_name",
        "roster_id",
        "draft_slot",
        "player_id",
        "player_name",
        "position",
        "nfl_team",
        "drafted_round",
        "drafted_pick_no",
        "drafted_by_roster_id",
    ]
    preferred_team = [
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

    # Decide header set
    any_player_rows = any("player_id" in r for r in rows)
    if any_player_rows:
        keys = set()
        for r in rows:
            keys.update(r.keys())
        fieldnames = [k for k in preferred_player if k in keys] + [k for k in sorted(keys) if k not in preferred_player]
    else:
        fieldnames = preferred_team

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


def write_xlsx(rows: List[Dict[str, str]], out_path: str) -> None:
    # Lazy import to avoid hard dependency when not needed
    try:
        from openpyxl import Workbook
    except Exception as e:
        raise SystemExit("openpyxl is required for .xlsx export. Install with: python3 -m pip install --user openpyxl")

    if not rows:
        headers: List[str] = []
    else:
        # Union of keys across rows, but keep a stable preferred order
        preferred = [
            "season",
            "league_name",
            "league_id",
            "team_name",
            "owner_display_name",
            "roster_id",
            "draft_slot",
            "player_id",
            "player_name",
            "position",
            "nfl_team",
            "drafted_round",
            "drafted_pick_no",
            "drafted_by_roster_id",
        ]
        keys = set()
        for r in rows:
            keys.update(r.keys())
        headers = [k for k in preferred if k in keys] + [k for k in sorted(keys) if k not in preferred]

    wb = Workbook()
    ws = wb.active
    ws.title = "players"

    if headers:
        ws.append(headers)
        for r in rows:
            ws.append([r.get(h, "") for h in headers])

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    wb.save(out_path)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Sleeper leagues roster and draft position to CSV or Excel",
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--username", help="Sleeper username to export for")
    g.add_argument("--user-id", help="Sleeper user_id to export for")
    g.add_argument("--league-id", help="Sleeper league_id to export (exports all rosters)")
    parser.add_argument("--season", help="Season (e.g. 2024). Defaults to active league season.")
    parser.add_argument("--names", action="store_true", help="Include player names (calls large /players endpoint)")
    parser.add_argument(
        "--players",
        action="store_true",
        help="When used with --league-id and CSV output, writes one row per player including draft round/pick",
    )
    parser.add_argument("--out", help="Output path. If ends with .xlsx, writes an Excel file of players; otherwise CSV summary.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    if args.league_id:
        league_id = str(args.league_id).strip().strip("/")
        # If .xlsx requested, produce per-player rows including draft round/pick
        if args.out and args.out.lower().endswith(".xlsx"):
            rows = build_player_rows_for_league(league_id=league_id, include_names=args.names)
            write_xlsx(rows, args.out)
            return
        # For CSV: if --players, output per-player rows; else team summary
        if args.players:
            rows = build_player_rows_for_league(league_id=league_id, include_names=args.names)
            write_csv(rows, args.out)
            return
        rows = build_export_rows_for_league(league_id=league_id, include_names=args.names)
        write_csv(rows, args.out)
        return

    user_id = resolve_user_id(args.username, args.user_id)
    season = args.season or get_active_league_season()

    rows = build_export_rows(user_id=user_id, season=season, include_names=args.names)

    write_csv(rows, args.out)


if __name__ == "__main__":
    main()