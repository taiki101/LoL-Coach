import hashlib
import heapq
import httpx
import json
import os
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

import plotly.express as px
import requests
import streamlit as st
from google import genai
from google.genai.errors import APIError
from google.genai._gaos.lib.compat_errors import APIError as InteractionsAPIError
from requests.exceptions import RequestException
from riotwatcher import ApiError, LolWatcher, RiotWatcher
from streamlit.errors import StreamlitSecretNotFoundError


st.set_page_config(
    page_title="LoL AI Coach",
    page_icon="⚔️",
    layout="wide",
)

MODEL_NAME = "gemini-3.1-flash-lite"
PLATFORM_REGIONS = ["JP1", "KR", "NA1", "EUW1", "EUN1", "BR1", "LA1", "LA2", "OC1", "TR1", "RU"]
DEFAULT_PLATFORM = "JP1"
MATCH_COUNT = 20
CLASSIC_QUEUE_IDS = (400, 420, 430, 440, 490)
QUEUE_FILTERS = {
    "ソロランク": (420,),
    "ノーマル": (400, 430, 490),
    "すべて": CLASSIC_QUEUE_IDS,
}
ROLE_LABELS = {
    "TOP": "トップ",
    "JUNGLE": "ジャングル",
    "MIDDLE": "ミッド",
    "BOTTOM": "ボット",
    "UTILITY": "サポート",
}
CACHE_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "riot_cache.sqlite3")
RIOT_RATE_LIMITS = ((20, 1.0), (100, 120.0))
ACCOUNT_CACHE_TTL = 24 * 60 * 60
MATCHLIST_CACHE_TTL = 5 * 60
MATCH_CACHE_TTL = 30 * 24 * 60 * 60
TIMELINE_CACHE_TTL = 30 * 24 * 60 * 60
REGIONAL_ROUTES = {
    "BR1": "americas",
    "EUN1": "europe",
    "EUW1": "europe",
    "JP1": "asia",
    "KR": "asia",
    "LA1": "americas",
    "LA2": "americas",
    "NA1": "americas",
    "OC1": "sea",
    "RU": "europe",
    "TR1": "europe",
}
DEFAULT_SYSTEM_PROMPT = """あなたはソロランクで勝てずに悩むプレイヤーを支える、親身で実践的な専属コーチです。
責めたり曖昧な精神論に逃げたりせず、提供された試合データから確認できる事実を根拠に、次の試合で実行できる助言をしてください。
回答はすべて自然な日本語にしてください。データから判断できない内容は断定せず、判断材料が足りないと明記してください。"""


def initialize_state() -> None:
    defaults = {
        "matches_data": [],
        "matches_puuid": None,
        "selected_match_id": None,
        "timelines_data": {},
        "timeline_raw_data": {},
        "chat_history": [],
        "chat_match_id": None,
        "coaching_result": None,
        "coaching_match_id": None,
        "settings_fingerprint": None,
        "account_search_results": [],
        "account_search_results_query": "",
        "pending_riot_id": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def account_route(platform: str) -> str:
    if platform not in PLATFORM_REGIONS:
        raise ValueError(f"未対応のプラットフォームです: {platform}")
    return REGIONAL_ROUTES[platform]


def configured_gemini_api_key() -> str:
    environment_key = os.getenv("GEMINI_API_KEY", "").strip()
    if environment_key:
        return environment_key
    try:
        return str(st.secrets.get("GEMINI_API_KEY", "")).strip()
    except StreamlitSecretNotFoundError:
        return ""


def configured_riot_api_key() -> str:
    environment_key = os.getenv("RIOT_API_KEY", "").strip()
    if environment_key:
        return environment_key
    try:
        return str(st.secrets.get("RIOT_API_KEY", "")).strip()
    except StreamlitSecretNotFoundError:
        return ""


def parse_riot_id(riot_id: str) -> tuple[str, str]:
    game_name, separator, tag_line = riot_id.partition("#")
    if not separator or not game_name.strip() or not tag_line.strip():
        raise ValueError("Riot ID は GameName#TAG の形式で入力してください。")
    return game_name.strip(), tag_line.strip()


@st.cache_resource(show_spinner=False)
def initialize_riot_cache() -> bool:
    connection = sqlite3.connect(CACHE_DB_PATH, timeout=30)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS riot_api_cache (
                cache_key TEXT PRIMARY KEY,
                cached_at REAL NOT NULL,
                response_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS riot_api_requests (
                api_key_hash TEXT NOT NULL,
                requested_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_riot_api_requests_key_time
            ON riot_api_requests (api_key_hash, requested_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS riot_players (
                puuid TEXT PRIMARY KEY,
                game_name TEXT NOT NULL,
                game_name_normalized TEXT NOT NULL,
                tag_line TEXT NOT NULL,
                tag_line_normalized TEXT NOT NULL,
                platform TEXT NOT NULL,
                indexed_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_riot_players_name
            ON riot_players (game_name_normalized, tag_line_normalized)
            """
        )
        connection.commit()
    finally:
        connection.close()
    return True


@contextmanager
def riot_cache_connection() -> Iterator[sqlite3.Connection]:
    initialize_riot_cache()
    connection = sqlite3.connect(CACHE_DB_PATH, timeout=30)
    try:
        yield connection
    finally:
        connection.close()


def reserve_riot_api_request(api_key: str) -> None:
    api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    while True:
        wait_seconds = 0.0
        with riot_cache_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            connection.execute(
                "DELETE FROM riot_api_requests WHERE requested_at <= ?",
                (now - RIOT_RATE_LIMITS[-1][1],),
            )
            counts = [
                connection.execute(
                    """
                    SELECT COUNT(*) FROM riot_api_requests
                    WHERE api_key_hash = ? AND requested_at > ?
                    """,
                    (api_key_hash, now - window_seconds),
                ).fetchone()[0]
                for _, window_seconds in RIOT_RATE_LIMITS
            ]
            exceeded = [
                (limit, window_seconds, count)
                for (limit, window_seconds), count in zip(RIOT_RATE_LIMITS, counts)
                if count >= limit
            ]
            if not exceeded:
                connection.execute(
                    "INSERT INTO riot_api_requests (api_key_hash, requested_at) VALUES (?, ?)",
                    (api_key_hash, now),
                )
                connection.commit()
                return

            waits = []
            for _, window_seconds, _ in exceeded:
                oldest_request = connection.execute(
                    """
                    SELECT MIN(requested_at) FROM riot_api_requests
                    WHERE api_key_hash = ? AND requested_at > ?
                    """,
                    (api_key_hash, now - window_seconds),
                ).fetchone()[0]
                if oldest_request is not None:
                    waits.append(oldest_request + window_seconds - now)
            connection.rollback()
            wait_seconds = max(waits, default=0.05)
        time.sleep(max(wait_seconds, 0.05) + 0.01)


def cached_riot_api_call(
    api_key: str,
    cache_key: str,
    ttl_seconds: int,
    request: Callable[[], Any],
) -> Any:
    with riot_cache_connection() as connection:
        cached = connection.execute(
            "SELECT cached_at, response_json FROM riot_api_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    if cached and time.time() - cached[0] < ttl_seconds:
        return json.loads(cached[1])

    reserve_riot_api_request(api_key)
    response = request()
    response_json = json.dumps(response, ensure_ascii=False)
    with riot_cache_connection() as connection:
        connection.execute(
            """
            INSERT INTO riot_api_cache (cache_key, cached_at, response_json)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                cached_at = excluded.cached_at,
                response_json = excluded.response_json
            """,
            (cache_key, time.time(), response_json),
        )
        connection.commit()
    return response


def store_riot_players(
    players: list[tuple[str, str, str, str]],
) -> None:
    indexed_at = int(time.time())
    records = [
        (
            puuid,
            game_name,
            game_name.casefold(),
            tag_line,
            tag_line.casefold(),
            platform,
            indexed_at,
        )
        for puuid, game_name, tag_line, platform in players
        if puuid and game_name and tag_line
    ]
    if not records:
        return
    with riot_cache_connection() as connection:
        connection.executemany(
            """
            INSERT INTO riot_players (
                puuid, game_name, game_name_normalized, tag_line,
                tag_line_normalized, platform, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(puuid) DO UPDATE SET
                game_name = excluded.game_name,
                game_name_normalized = excluded.game_name_normalized,
                tag_line = excluded.tag_line,
                tag_line_normalized = excluded.tag_line_normalized,
                platform = excluded.platform,
                indexed_at = excluded.indexed_at
            """,
            records,
        )
        connection.commit()


def search_riot_players(query: str, limit: int = 30) -> list[dict[str, str]]:
    normalized_query = query.strip().casefold()
    if not normalized_query:
        return []
    with riot_cache_connection() as connection:
        rows = connection.execute(
            """
            SELECT puuid, game_name, tag_line, platform
            FROM riot_players
            WHERE instr(game_name_normalized, ?)
                OR instr(tag_line_normalized, ?)
                OR instr(game_name_normalized || '#' || tag_line_normalized, ?)
            ORDER BY game_name_normalized, tag_line_normalized
            LIMIT ?
            """,
            (normalized_query, normalized_query, normalized_query, limit),
        ).fetchall()
    return [
        {
            "puuid": row[0],
            "game_name": row[1],
            "tag_line": row[2],
            "platform": row[3],
        }
        for row in rows
    ]


def select_riot_player(riot_id: str) -> None:
    st.session_state.pending_riot_id = riot_id
    st.session_state.account_search_results = []


def player_position(participant: dict[str, Any]) -> str:
    return participant.get("teamPosition") or participant.get("individualPosition") or "不明"


def kda_ratio(participant: dict[str, Any]) -> float:
    return (participant.get("kills", 0) + participant.get("assists", 0)) / max(
        participant.get("deaths", 0), 1
    )


def cs_count(participant: dict[str, Any]) -> int:
    return participant.get("totalMinionsKilled", 0) + participant.get(
        "neutralMinionsKilled", 0
    )


def get_team(match_info: dict[str, Any], team_id: int) -> dict[str, Any]:
    for team in match_info.get("teams", []):
        if team.get("teamId") == team_id:
            return team
    return {}


def participant_summary(participant: dict[str, Any]) -> dict[str, Any]:
    return {
        "champion": participant.get("championName", "不明"),
        "role": player_position(participant),
        "kills": participant.get("kills", 0),
        "deaths": participant.get("deaths", 0),
        "assists": participant.get("assists", 0),
        "goldEarned": participant.get("goldEarned", 0),
        "damageDealt": participant.get("totalDamageDealtToChampions", 0),
        "visionScore": participant.get("visionScore", 0),
        "cs": cs_count(participant),
    }


def build_match_summary(
    match: dict[str, Any], puuid: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    info = match.get("info", {})
    participants = info.get("participants", [])
    player = next((p for p in participants if p.get("puuid") == puuid), None)
    if player is None:
        raise ValueError("取得した試合データに対象プレイヤーが含まれていません。")

    duration_seconds = info.get("gameDuration", 0)
    duration_minutes = duration_seconds / 60 if duration_seconds > 0 else 0
    team_id = player.get("teamId")
    allies = [p for p in participants if p.get("teamId") == team_id]
    opponents = [p for p in participants if p.get("teamId") != team_id]
    position = player_position(player)
    lane_opponent = next(
        (p for p in opponents if position != "不明" and player_position(p) == position),
        None,
    )
    own_team = get_team(info, team_id)
    objectives = own_team.get("objectives", {})
    total_team_kills = sum(p.get("kills", 0) for p in allies)
    total_team_gold = sum(p.get("goldEarned", 0) for p in allies)

    player_cs = cs_count(player)
    items = [str(player.get(f"item{index}", 0)) for index in range(6) if player.get(f"item{index}", 0)]
    summary = {
        "gameDurationMinutes": round(duration_minutes, 1),
        "queueId": info.get("queueId"),
        "gameMode": info.get("gameMode"),
        "win": bool(player.get("win", False)),
        "targetPlayer": {
            "champion": player.get("championName", "不明"),
            "role": position,
            "kills": player.get("kills", 0),
            "deaths": player.get("deaths", 0),
            "assists": player.get("assists", 0),
            "kda": f"{kda_ratio(player):.2f}",
            "cs": player_cs,
            "csPerMin": round(player_cs / duration_minutes, 2) if duration_minutes else 0,
            "goldEarned": player.get("goldEarned", 0),
            "damageDealt": player.get("totalDamageDealtToChampions", 0),
            "visionScore": player.get("visionScore", 0),
            "items": items,
        },
        "laneOpponent": (
            {
                "champion": lane_opponent.get("championName", "不明"),
                "kills": lane_opponent.get("kills", 0),
                "deaths": lane_opponent.get("deaths", 0),
                "assists": lane_opponent.get("assists", 0),
                "goldEarned": lane_opponent.get("goldEarned", 0),
                "damageDealt": lane_opponent.get("totalDamageDealtToChampions", 0),
            }
            if lane_opponent
            else None
        ),
        "teamStats": {
            "totalKills": total_team_kills,
            "totalGold": total_team_gold,
            "dragons": objectives.get("dragon", {}).get("kills", 0),
            "barons": objectives.get("baron", {}).get("kills", 0),
        },
        "allies": [participant_summary(p) for p in allies if p.get("puuid") != puuid],
        "opponents": [participant_summary(p) for p in opponents],
    }
    return summary, player, allies, opponents


def match_datetime(match: dict[str, Any]) -> str:
    timestamp = match.get("info", {}).get("gameEndTimestamp", 0)
    if not timestamp:
        timestamp = match.get("info", {}).get("gameCreation", 0)
    if not timestamp:
        return "不明"
    return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M"
    )


def match_label(match: dict[str, Any], puuid: str) -> str:
    info = match.get("info", {})
    player = next((p for p in info.get("participants", []) if p.get("puuid") == puuid), {})
    result = "勝利" if player.get("win") else "敗北"
    return f"{match_datetime(match)} | {result} | {player.get('championName', '不明')} | {match.get('metadata', {}).get('matchId', '')}"


def build_recent_stats(matches: list[dict[str, Any]], puuid: str) -> dict[str, Any]:
    games: list[tuple[int, dict[str, Any]]] = []
    for match in matches:
        info = match.get("info", {})
        player = next(
            (p for p in info.get("participants", []) if p.get("puuid") == puuid), None
        )
        if player is not None:
            games.append((info.get("gameDuration", 0), player))
    if not games:
        return {}

    wins = sum(1 for _, player in games if player.get("win"))
    cs_rates = [
        cs_count(player) / (duration / 60) for duration, player in games if duration > 0
    ]
    role_records: dict[str, list[int]] = {}
    for _, player in games:
        role = ROLE_LABELS.get(player_position(player), "不明")
        record = role_records.setdefault(role, [0, 0])
        record[0] += 1
        record[1] += 1 if player.get("win") else 0
    role_rows = [
        {
            "ポジション": role,
            "試合数": games_count,
            "勝率": f"{role_wins / games_count * 100:.0f}%",
        }
        for role, (games_count, role_wins) in sorted(
            role_records.items(), key=lambda item: -item[1][0]
        )
    ]
    champion_records: dict[str, list[float]] = {}
    for _, player in games:
        record = champion_records.setdefault(
            player.get("championName", "不明"), [0, 0, 0.0]
        )
        record[0] += 1
        record[1] += 1 if player.get("win") else 0
        record[2] += kda_ratio(player)
    champion_rows = [
        {
            "チャンピオン": name,
            "試合": int(count),
            "勝率": f"{champion_wins / count * 100:.0f}%",
            "KDA": f"{kda_total / count:.2f}",
        }
        for name, (count, champion_wins, kda_total) in sorted(
            champion_records.items(), key=lambda item: -item[1][0]
        )[:8]
    ]
    trend = [
        {
            "試合": str(index),
            "KDA": round(kda_ratio(player), 2),
            "結果": "勝利" if player.get("win") else "敗北",
        }
        for index, (_, player) in enumerate(reversed(games), start=1)
    ]
    return {
        "games": len(games),
        "wins": wins,
        "winRate": wins / len(games) * 100,
        "avgKda": sum(kda_ratio(player) for _, player in games) / len(games),
        "avgCsPerMin": sum(cs_rates) / len(cs_rates) if cs_rates else 0.0,
        "topChampions": Counter(
            player.get("championName", "不明") for _, player in games
        ).most_common(3),
        "roles": role_rows,
        "championRows": champion_rows,
        "trend": trend,
    }


def match_id(match: dict[str, Any]) -> str:
    return str(match.get("metadata", {}).get("matchId", ""))


def match_timestamp(match: dict[str, Any]) -> int:
    info = match.get("info", {})
    return info.get("gameEndTimestamp") or info.get("gameCreation") or 0


def riot_error_message(error: ApiError) -> str:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status in (401, 403):
        return "Riot APIキーが無効、または有効期限（24時間）が切れています。Riot開発者ポータルで新しいキーを取得してください。"
    if status == 404:
        return "指定された Riot ID（GameName#TAG）が見つかりませんでした。表記とリージョンを確認してください。"
    if status == 429:
        return "Riot API の利用上限に達しました。しばらく待ってから再試行してください。"
    return f"Riot API エラーが発生しました（HTTP {status or '不明'}）。時間をおいて再試行してください。"


def fetch_matches(
    api_key: str,
    riot_id: str,
    platform: str,
    queue_ids: tuple[int, ...] = CLASSIC_QUEUE_IDS,
    count: int = MATCH_COUNT,
) -> tuple[str, list[dict[str, Any]]]:
    game_name, tag_line = parse_riot_id(riot_id)
    riot_watcher = RiotWatcher(api_key)
    lol_watcher = LolWatcher(api_key)
    regional_route = account_route(platform)
    account = cached_riot_api_call(
        api_key,
        json.dumps(["account", regional_route, game_name.casefold(), tag_line.casefold()]),
        ACCOUNT_CACHE_TTL,
        lambda: riot_watcher.account.by_riot_id(regional_route, game_name, tag_line),
    )
    puuid = account["puuid"]
    store_riot_players(
        [
            (
                puuid,
                account.get("gameName") or game_name,
                account.get("tagLine") or tag_line,
                platform,
            )
        ]
    )
    queue_match_ids = {
        queue_id: cached_riot_api_call(
            api_key,
            json.dumps(["matchlist", regional_route, puuid, count, queue_id]),
            MATCHLIST_CACHE_TTL,
            lambda queue_id=queue_id: lol_watcher.match.matchlist_by_puuid(
                regional_route, puuid, count=count, queue=queue_id
            ),
        )
        for queue_id in queue_ids
    }
    progress = st.progress(0, text="選択したキューの試合を検索しています...")
    queue_positions = dict.fromkeys(queue_ids, 0)
    seen_match_ids: set[str] = set()
    match_by_id: dict[str, dict[str, Any]] = {}
    newest_matches: list[tuple[int, int, str]] = []
    selected_matches: list[dict[str, Any]] = []

    def load_next_match(queue_id: int) -> None:
        match_ids = queue_match_ids[queue_id]
        while queue_positions[queue_id] < len(match_ids):
            match_key = match_ids[queue_positions[queue_id]]
            queue_positions[queue_id] += 1
            if match_key in seen_match_ids:
                continue
            seen_match_ids.add(match_key)
            match = cached_riot_api_call(
                api_key,
                json.dumps(["match", regional_route, match_key]),
                MATCH_CACHE_TTL,
                lambda: lol_watcher.match.by_id(regional_route, match_key),
            )
            info = match.get("info", {})
            if (
                info.get("queueId") not in queue_ids
                or info.get("gameMode") != "CLASSIC"
            ):
                continue
            match_by_id[match_key] = match
            heapq.heappush(
                newest_matches,
                (-match_timestamp(match), queue_id, match_key),
            )
            return

    for queue_id in queue_ids:
        load_next_match(queue_id)

    while newest_matches and len(selected_matches) < count:
        _, queue_id, selected_id = heapq.heappop(newest_matches)
        selected_matches.append(match_by_id.pop(selected_id))
        progress.progress(
            len(selected_matches) / count,
            text=f"試合を取得中... ({len(selected_matches)}/{count})",
        )
        if len(selected_matches) < count:
            load_next_match(queue_id)
    progress.empty()
    discovered_players = []
    for match in selected_matches:
        for participant in match.get("info", {}).get("participants", []):
            discovered_players.append(
                (
                    participant.get("puuid", ""),
                    participant.get("riotIdGameName", ""),
                    participant.get("riotIdTagline", ""),
                    platform,
                )
            )
    store_riot_players(discovered_players)
    return puuid, selected_matches


def build_timeline_summary(
    timeline: dict[str, Any], match: dict[str, Any], puuid: str
) -> list[str]:
    participants = match.get("info", {}).get("participants", [])
    player = next((p for p in participants if p.get("puuid") == puuid), None)
    if player is None:
        raise ValueError("試合タイムラインに対象プレイヤーが見つかりません。")

    target_id = player.get("participantId")
    participant_names = {
        p.get("participantId"): p.get("championName", "味方")
        for p in participants
    }
    important_events = []
    for frame in timeline.get("info", {}).get("frames", []):
        for event in frame.get("events", []):
            event_type = event.get("type")
            timestamp = event.get("timestamp", 0)
            time_label = f"{timestamp // 60000}:{timestamp // 1000 % 60:02}"
            if event_type == "CHAMPION_KILL":
                killer_id = event.get("killerId")
                victim_id = event.get("victimId")
                assists = event.get("assistingParticipantIds", [])
                if target_id not in (killer_id, victim_id) and target_id not in assists:
                    continue
                if killer_id == target_id:
                    description = f"自分が{participant_names.get(victim_id, '敵')}をキル"
                elif victim_id == target_id:
                    description = f"{participant_names.get(killer_id, '敵')}に倒された"
                else:
                    description = f"{participant_names.get(killer_id, '味方')}のキルに参加"
                important_events.append(f"{time_label} - {description}")
            elif event_type == "ELITE_MONSTER_KILL":
                monster = event.get("monsterType", "大型モンスター")
                sub_type = event.get("monsterSubType")
                if sub_type:
                    monster = f"{monster}（{sub_type}）"
                killer = participant_names.get(event.get("killerId"), "味方")
                important_events.append(f"{time_label} - {killer}が{monster}を討伐")
            elif event_type == "BUILDING_KILL":
                building = event.get("buildingType", "建造物")
                lane = event.get("laneType", "レーン")
                team = "味方側" if event.get("teamId") == player.get("teamId") else "敵側"
                important_events.append(f"{time_label} - {team}の{lane}で{building}が破壊")
            elif event_type == "TURRET_PLATE_DESTROYED" and event.get("killerId") == target_id:
                important_events.append(f"{time_label} - 自分がタワープレートを獲得")
    return important_events[:24]


def get_match_timeline(
    api_key: str, platform: str, selected_match_id: str
) -> dict[str, Any]:
    if selected_match_id not in st.session_state.timeline_raw_data:
        regional_route = account_route(platform)
        st.session_state.timeline_raw_data[selected_match_id] = cached_riot_api_call(
            api_key,
            json.dumps(["timeline", regional_route, selected_match_id]),
            TIMELINE_CACHE_TTL,
            lambda: LolWatcher(api_key).match.timeline_by_match(
                regional_route, selected_match_id
            ),
        )
    return st.session_state.timeline_raw_data[selected_match_id]


def build_gold_progress(
    timeline: dict[str, Any], match: dict[str, Any], puuid: str
) -> list[dict[str, Any]]:
    participants = match.get("info", {}).get("participants", [])
    target = next((p for p in participants if p.get("puuid") == puuid), None)
    if target is None:
        raise ValueError("試合データから対象プレイヤーを確認できませんでした。")

    team_ids = {p.get("teamId") for p in participants if p.get("teamId") is not None}
    team_player_ids = {
        team_id: [
            str(p.get("participantId"))
            for p in participants
            if p.get("teamId") == team_id and p.get("participantId") is not None
        ]
        for team_id in team_ids
    }
    result = []
    for frame in timeline.get("info", {}).get("frames", []):
        participant_frames = frame.get("participantFrames", {})
        minute = round(frame.get("timestamp", 0) / 60000, 1)
        for team_id, participant_ids in team_player_ids.items():
            total_gold = sum(
                participant_frames.get(participant_id, {}).get("totalGold", 0)
                for participant_id in participant_ids
            )
            result.append(
                {
                    "試合時間（分）": minute,
                    "チーム": "味方" if team_id == target.get("teamId") else "対戦相手",
                    "合計ゴールド": total_gold,
                }
            )
    return result


@st.cache_data(ttl=86400, show_spinner=False)
def get_ddragon_version() -> str:
    response = requests.get(
        "https://ddragon.leagueoflegends.com/api/versions.json", timeout=10
    )
    response.raise_for_status()
    versions = response.json()
    if not isinstance(versions, list) or not versions or not isinstance(versions[0], str):
        raise ValueError("Data Dragon のバージョン情報が正しくありません。")
    return versions[0]


def champion_image(champion: str, version: str) -> str:
    return (
        f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/"
        f"{champion}.png"
    )


def item_image(item_id: str, version: str) -> str:
    return f"https://ddragon.leagueoflegends.com/cdn/{version}/img/item/{item_id}.png"


def select_match(match_key: str) -> None:
    st.session_state.selected_match_id = match_key


def clear_match_state() -> None:
    st.session_state.matches_data = []
    st.session_state.matches_puuid = None
    st.session_state.selected_match_id = None
    st.session_state.timelines_data = {}
    st.session_state.timeline_raw_data = {}
    st.session_state.chat_history = []
    st.session_state.chat_match_id = None
    st.session_state.coaching_result = None
    st.session_state.coaching_match_id = None


def queue_label(queue_id: int) -> str:
    return {
        400: "ノーマル（ドラフト）",
        420: "ソロ／デュオランク",
        430: "ノーマル（ブラインド）",
        440: "フレックスランク",
        490: "ノーマル（クイックプレイ）",
    }.get(queue_id, "サモナーズリフト")


def render_team_panel(
    title: str, participants: list[dict[str, Any]], version: str | None
) -> None:
    with st.container(border=True):
        st.markdown(f"**{title}**")
        if not participants:
            st.caption("参加者データがありません")
            return
        columns = st.columns(len(participants))
        for column, participant in zip(columns, participants):
            with column:
                champion = participant.get("championName", "不明")
                if version:
                    st.image(champion_image(champion, version), width=56)
                st.caption(
                    f"{champion}\n"
                    f"{participant.get('kills', 0)}/"
                    f"{participant.get('deaths', 0)}/"
                    f"{participant.get('assists', 0)}"
                )


CHAT_SYSTEM_PROMPT = """あなたは親切で実践的なLeague of Legendsコーチです。
質問に直接答え、試合データで確認できる事実と推測を区別してください。
説明は簡潔にし、次の試合で試せる行動があれば具体的に提案してください。"""


def make_gemini_response(api_key: str, prompt: str) -> Iterator[str]:
    client = genai.Client(api_key=api_key)
    try:
        events = client.interactions.create(
            model=MODEL_NAME,
            input=prompt,
            generation_config={"thinking_level": "low"},
            store=False,
            stream=True,
            timeout=120_000,
        )
        for event in events:
            if event.event_type == "step.delta" and event.delta.type == "text":
                if event.delta.text:
                    yield event.delta.text
    except InteractionsAPIError as error:
        status_code = getattr(error, "status_code", None)
        if status_code == 503:
            message = "Gemini API が混雑しています（503）。しばらく待ってから再試行してください。"
        elif status_code == 429:
            message = "Gemini API の利用上限に達しました（429）。利用枠を確認し、時間をおいて再試行してください。"
        else:
            message = (
                "Gemini APIとの通信に失敗しました"
                f"（HTTP {status_code or '不明'}）。APIキーと利用状況を確認してください。"
            )
        raise ValueError(message) from error
    except httpx.TimeoutException as error:
        raise ValueError(
            "Gemini の応答が120秒以内に返りませんでした。時間をおいて再試行してください。"
        ) from error
    except httpx.RequestError as error:
        raise ValueError("Gemini APIとの通信に失敗しました。接続を確認してください。") from error


initialize_state()

COACH_AVATAR = "🎯"

st.markdown(
    """
    <style>
    .block-container {max-width: 1200px; padding-top: 1rem; padding-bottom: 2rem;}
    .hero {
        padding: 12px 20px;
        border-radius: 14px;
        background: linear-gradient(120deg, #1d3557 0%, #0f3460 55%, #16213e 100%);
        border: 1px solid rgba(200, 155, 60, .35);
        color: #f5f7fa;
        margin-bottom: .8rem;
    }
    .hero-title {font-size: 1.5rem; font-weight: 800; letter-spacing: .02em;}
    .hero-subtitle {color: #c9d6e3; margin-left: .8rem; font-size: .95rem; font-weight: 600;}
    .hero-note {color: #9fb6cd; margin-top: .2rem; font-size: .8rem;}
    .badge {
        display: inline-block;
        padding: 2px 12px;
        border-radius: 999px;
        font-weight: 700;
        font-size: .85rem;
    }
    .badge-win {
        background: rgba(46, 204, 113, .16);
        color: #2ecc71;
        border: 1px solid rgba(46, 204, 113, .55);
    }
    .badge-loss {
        background: rgba(255, 107, 107, .14);
        color: #ff6b6b;
        border: 1px solid rgba(255, 107, 107, .55);
    }
    .champion-cell {display: flex; align-items: center; gap: 10px; font-weight: 700;}
    .champion-cell img {width: 36px; height: 36px; border-radius: 8px;}
    [data-testid="stMetric"] {
        padding: 8px 12px;
        border-radius: 14px;
        border: 1px solid rgba(200, 155, 60, .25);
    }
    [data-testid="stVerticalBlockBorderWrapper"] {border-radius: 14px;}
    .muted {opacity: .65; font-size: .85rem;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="hero">
        <div class="hero-title">⚔️ LoL AI Coach<span class="hero-subtitle">ソロランク勝率UP 専属AIコーチ</span></div>
        <div class="hero-note">Riot ID を入力して戦績を同期すると、試合ごとの敗因・勝因と次の一手をAIコーチが提案します。</div>
    </div>
    """,
    unsafe_allow_html=True,
)

riot_api_key = configured_riot_api_key()
gemini_api_key = configured_gemini_api_key()
if not riot_api_key:
    st.error("Riot APIキーがサーバーに設定されていません。運営者に連絡してください。")
    st.stop()

platform = DEFAULT_PLATFORM
input_column, queue_column, button_column = st.columns(
    [3, 1.5, 1.7], vertical_alignment="bottom"
)
with input_column:
    riot_id = st.text_input(
        "Riot ID または名前の一部",
        placeholder="例：Player#JP1 ／ 名前の一部だけでも検索できます",
        key="riot_id_input",
    )
with queue_column:
    queue_type = st.selectbox(
        "キュータイプ",
        options=list(QUEUE_FILTERS),
        key="queue_type",
    )
with button_column:
    sync_clicked = st.button("🔄 最新の戦績を同期", type="primary", width="stretch")
selected_queue_ids = QUEUE_FILTERS[queue_type]

search_target_riot_id = st.session_state.pop("pending_riot_id", None)
if sync_clicked:
    if not riot_id.strip():
        st.error("Riot ID または名前を入力してください。")
    elif "#" in riot_id:
        st.session_state.account_search_results = []
        st.session_state.account_search_results_query = ""
        search_target_riot_id = riot_id.strip()
    else:
        try:
            results = search_riot_players(riot_id)
            st.session_state.account_search_results = results
            st.session_state.account_search_results_query = riot_id.strip()
            if not results:
                st.info(
                    "一致するプレイヤーが見つかりません。"
                    "まだ登録されていない場合は、完全なRiot ID（Player#TAG）で同期してください。"
                )
        except sqlite3.Error:
            st.error("プレイヤー検索データを読み込めませんでした。保存先を確認してください。")

if (
    st.session_state.account_search_results
    and st.session_state.account_search_results_query.casefold() == riot_id.strip().casefold()
):
    st.markdown("**検索結果（最大30件）**")
    for player in st.session_state.account_search_results:
        player_riot_id = f"{player['game_name']}#{player['tag_line']}"
        st.button(
            f"{player_riot_id} の戦績を同期",
            key=f"search_player_{player['puuid']}",
            on_click=select_riot_player,
            args=(player_riot_id,),
        )

if search_target_riot_id:
    settings_fingerprint = f"{search_target_riot_id}|{queue_type}"
    if st.session_state.settings_fingerprint != settings_fingerprint:
        clear_match_state()
        st.session_state.settings_fingerprint = settings_fingerprint
    try:
        with st.spinner("Riot APIから最新の戦績を同期しています..."):
            puuid, fetched_matches = fetch_matches(
                riot_api_key,
                search_target_riot_id,
                platform,
                selected_queue_ids,
                MATCH_COUNT,
            )
        st.session_state.matches_puuid = puuid
        st.session_state.matches_data = fetched_matches
        st.session_state.selected_match_id = (
            match_id(fetched_matches[0]) if fetched_matches else None
        )
        st.session_state.timelines_data = {}
        st.session_state.timeline_raw_data = {}
        st.session_state.chat_history = []
        st.session_state.chat_match_id = None
        st.session_state.coaching_result = None
        st.session_state.coaching_match_id = None
        if fetched_matches:
            st.success(f"{len(fetched_matches)}試合を同期しました。")
        else:
            st.info("対象となる試合が見つかりませんでした。キュータイプを変えて再度お試しください。")
    except ValueError as error:
        st.error(str(error))
    except ApiError as error:
        st.error(riot_error_message(error))
    except RequestException:
        st.error("Riot APIとの通信に失敗しました。接続を確認してください。")
    except sqlite3.Error:
        st.error("戦績キャッシュを保存できませんでした。保存先の権限を確認してください。")


DASHBOARD_PANEL_HEIGHT = 300


def render_summary_panel(stats: dict[str, Any], queue_label_text: str) -> None:
    with st.container(border=True, height=DASHBOARD_PANEL_HEIGHT):
        st.markdown(f"**📈 戦績サマリー**　{queue_label_text}・直近{stats['games']}試合")
        top_metrics = st.columns(2)
        top_metrics[0].metric(
            "勝率",
            f"{stats['winRate']:.1f}%",
            f"{stats['wins']}勝 {stats['games'] - stats['wins']}敗",
        )
        top_metrics[1].metric("平均KDA", f"{stats['avgKda']:.2f}")
        bottom_metrics = st.columns(2)
        bottom_metrics[0].metric("平均CS/分", f"{stats['avgCsPerMin']:.1f}")
        bottom_metrics[1].metric(
            "よく使うチャンピオン",
            stats["topChampions"][0][0] if stats["topChampions"] else "-",
        )


def render_trend_panel(trend: list[dict[str, Any]]) -> None:
    with st.container(border=True, height=DASHBOARD_PANEL_HEIGHT):
        st.markdown("**📉 試合ごとのKDA推移**　古い試合 → 新しい試合")
        figure = px.bar(
            trend,
            x="試合",
            y="KDA",
            color="結果",
            color_discrete_map={"勝利": "#2ecc71", "敗北": "#ff6b6b"},
            category_orders={"試合": [row["試合"] for row in trend]},
        )
        figure.update_layout(
            template="plotly_dark",
            height=DASHBOARD_PANEL_HEIGHT - 90,
            margin=dict(l=10, r=10, t=10, b=10),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1,
                title=None,
            ),
            xaxis_title=None,
            yaxis_title=None,
        )
        st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})


def render_champion_panel(stats: dict[str, Any]) -> None:
    with st.container(border=True, height=DASHBOARD_PANEL_HEIGHT):
        st.markdown("**🦸 チャンピオン別の成績**")
        st.dataframe(
            stats["championRows"],
            hide_index=True,
            width="stretch",
            height=DASHBOARD_PANEL_HEIGHT - 120,
        )
        if stats["roles"]:
            role_text = "・".join(
                f"{row['ポジション']} {row['試合数']}試合（勝率{row['勝率']}）"
                for row in stats["roles"]
            )
            st.caption(f"ポジション：{role_text}")


def render_match_list_panel(
    matches: list[dict[str, Any]],
    puuid: str,
    ddragon_version: str | None,
    queue_label_text: str,
) -> None:
    with st.container(border=True, height=DASHBOARD_PANEL_HEIGHT):
        st.markdown(f"**🗂️ 直近の試合一覧**　{queue_label_text}")
        for game_match in matches:
            game_match_id = match_id(game_match)
            game_info = game_match.get("info", {})
            game_player = next(
                (
                    participant
                    for participant in game_info.get("participants", [])
                    if participant.get("puuid") == puuid
                ),
                None,
            )
            if game_player is None:
                continue
            won = bool(game_player.get("win", False))
            badge_class = "badge-win" if won else "badge-loss"
            champion_name = game_player.get("championName", "不明")
            duration_seconds = game_info.get("gameDuration", 0)
            cs_per_minute = (
                cs_count(game_player) / (duration_seconds / 60)
                if duration_seconds > 0
                else 0
            )
            is_selected = game_match_id == st.session_state.selected_match_id
            (
                result_column,
                info_column,
                kda_column,
                action_column,
            ) = st.columns([1.1, 2.4, 1.5, 2.2], vertical_alignment="center")
            with result_column:
                st.markdown(
                    f'<span class="badge {badge_class}">{"勝利" if won else "敗北"}</span>',
                    unsafe_allow_html=True,
                )
            with info_column:
                image_html = (
                    f'<img src="{champion_image(champion_name, ddragon_version)}" alt="">'
                    if ddragon_version
                    else ""
                )
                st.markdown(
                    f'<div class="champion-cell">{image_html}<span>{champion_name}</span></div>',
                    unsafe_allow_html=True,
                )
                st.caption(
                    f"{queue_label(game_info.get('queueId', 0))}・"
                    f"{duration_seconds // 60}分・{match_datetime(game_match)}"
                )
            with kda_column:
                st.markdown(
                    f"**{game_player.get('kills', 0)}/"
                    f"{game_player.get('deaths', 0)}/"
                    f"{game_player.get('assists', 0)}**"
                )
                st.caption(f"CS/分 {cs_per_minute:.1f}")
            with action_column:
                st.button(
                    "🔍 コーチングを受ける",
                    key=f"select_match_{game_match_id}",
                    on_click=select_match,
                    args=(game_match_id,),
                    type="primary" if is_selected else "secondary",
                    width="stretch",
                )


matches = st.session_state.matches_data
puuid = st.session_state.matches_puuid
if matches and puuid:
    match_by_id = {match_id(match): match for match in matches}
    latest_match_id = match_id(matches[0])
    if st.session_state.selected_match_id not in match_by_id:
        st.session_state.selected_match_id = latest_match_id

    try:
        ddragon_version = get_ddragon_version()
    except (RequestException, ValueError):
        ddragon_version = None
        st.warning("チャンピオン画像を読み込めませんでした。時間をおいて再読み込みしてください。")

    recent_stats = build_recent_stats(matches, puuid)
    if recent_stats:
        st.caption(
            f"{queue_type}の直近{len(matches)}試合。「🔍 コーチングを受ける」で下のタブに詳細とAIコーチングを表示します。"
        )
        summary_column, trend_column = st.columns([1, 1.5], gap="small")
        with summary_column:
            render_summary_panel(recent_stats, queue_type)
        with trend_column:
            render_trend_panel(recent_stats["trend"])
        champion_column, list_column = st.columns([1, 1.5], gap="small")
        with champion_column:
            render_champion_panel(recent_stats)
        with list_column:
            render_match_list_panel(matches, puuid, ddragon_version, queue_type)
    selected_match_id = st.session_state.selected_match_id
    selected_match = match_by_id[selected_match_id]
    if selected_match_id != latest_match_id:
        st.info(f"過去の試合を表示中：{match_label(selected_match, puuid)}")
        st.button(
            "最新の試合に戻る",
            on_click=select_match,
            args=(latest_match_id,),
            key="return_to_latest",
        )

    if st.session_state.chat_match_id != selected_match_id:
        st.session_state.chat_history = []
        st.session_state.chat_match_id = selected_match_id
    if st.session_state.coaching_match_id != selected_match_id:
        st.session_state.coaching_result = None
        st.session_state.coaching_match_id = selected_match_id

    summary, target_player, allies, opponents = build_match_summary(selected_match, puuid)
    result_text = "勝利" if target_player.get("win") else "敗北"
    result_color = "#2ecc71" if target_player.get("win") else "#ff6b6b"
    tabs = st.tabs(
        [
            "📊 戦績データ・比較",
            "🎯 専属AIコーチング（勝つための具体改善案）",
            "💬 コーチと1対1で相談",
        ]
    )

    with tabs[0]:
        hero_columns = st.columns([1, 5])
        with hero_columns[0]:
            if ddragon_version:
                st.image(
                    champion_image(
                        target_player.get("championName", "Teemo"), ddragon_version
                    ),
                    width=112,
                )
        with hero_columns[1]:
            st.markdown(f"## {target_player.get('championName', '不明')} · {result_text}")
            st.caption(
                f"{match_datetime(selected_match)}　·　"
                f"{queue_label(summary.get('queueId', 0))}　·　"
                f"{summary['gameDurationMinutes']:.1f}分　·　"
                f"{player_position(target_player)}"
            )
        st.markdown(
            f"<div style='height:4px;border-radius:8px;background:{result_color};"
            "margin:4px 0 20px 0'></div>",
            unsafe_allow_html=True,
        )

        metrics = st.columns(5)
        metrics[0].metric(
            "KDA",
            f"{target_player.get('kills', 0)}/"
            f"{target_player.get('deaths', 0)}/"
            f"{target_player.get('assists', 0)}",
            summary["targetPlayer"]["kda"],
        )
        metrics[1].metric("CS/分", f"{summary['targetPlayer']['csPerMin']:.1f}")
        metrics[2].metric(
            "チャンピオンへの与ダメージ",
            f"{target_player.get('totalDamageDealtToChampions', 0):,}",
        )
        metrics[3].metric("視界スコア", f"{target_player.get('visionScore', 0):,}")
        metrics[4].metric("獲得ゴールド", f"{target_player.get('goldEarned', 0):,}")

        team_columns = st.columns(2)
        with team_columns[0]:
            render_team_panel("味方チーム", allies, ddragon_version)
        with team_columns[1]:
            render_team_panel("対戦チーム", opponents, ddragon_version)

        item_ids = [
            str(target_player.get(f"item{index}", 0))
            for index in range(6)
            if target_player.get(f"item{index}", 0)
        ]
        if item_ids:
            st.markdown("**ビルド**")
            item_columns = st.columns(6)
            for column, item_id in zip(item_columns, item_ids):
                with column:
                    if ddragon_version:
                        st.image(item_image(item_id, ddragon_version), width=48)
                    st.caption(f"アイテム番号：{item_id}")

        chart_columns = st.columns(2)
        damage_values = [
            {
                "チャンピオン": participant.get("championName", "不明"),
                "与ダメージ": participant.get("totalDamageDealtToChampions", 0),
            }
            for participant in allies
        ]
        gold_values = [
            {
                "チャンピオン": participant.get("championName", "不明"),
                "陣営": "味方" if participant.get("teamId") == target_player.get("teamId") else "敵",
                "獲得ゴールド": participant.get("goldEarned", 0),
            }
            for participant in selected_match.get("info", {}).get("participants", [])
        ]
        with chart_columns[0]:
            st.markdown("**味方チームの与ダメージ**")
            if any(row["与ダメージ"] for row in damage_values):
                st.plotly_chart(
                    px.pie(
                        damage_values,
                        names="チャンピオン",
                        values="与ダメージ",
                        hole=0.55,
                    ),
                    width="stretch",
                )
        with chart_columns[1]:
            st.markdown("**チーム別ゴールド**")
            st.plotly_chart(
                px.bar(
                    gold_values,
                    x="チャンピオン",
                    y="獲得ゴールド",
                    color="陣営",
                    barmode="group",
                ),
                width="stretch",
            )

        st.markdown("**試合中のゴールド推移**")
        try:
            with st.spinner("試合のゴールド推移を取得しています..."):
                timeline = get_match_timeline(riot_api_key, platform, selected_match_id)
            gold_progress = build_gold_progress(timeline, selected_match, puuid)
            if gold_progress:
                st.plotly_chart(
                    px.line(
                        gold_progress,
                        x="試合時間（分）",
                        y="合計ゴールド",
                        color="チーム",
                        markers=True,
                    ),
                    width="stretch",
                )
            else:
                st.info("ゴールド推移を表示できるデータがありません。")
        except ApiError as error:
            st.error(riot_error_message(error))
        except sqlite3.Error:
            st.error("ゴールド推移を読み込めませんでした。保存先を確認してください。")
        except RequestException:
            st.error("試合の時間軸を取得できませんでした。接続を確認してください。")
        except ValueError as error:
            st.error(str(error))

    with tabs[1]:
        st.markdown(
            "試合の統計と時間軸をもとに、勝敗を分けた場面と次の試合の行動を具体的に分析します。"
        )
        if not gemini_api_key:
            st.error("AIコーチ機能は未設定です。運営者にGemini APIキーの設定を依頼してください。")
        coach_clicked = st.button(
            "コーチングを生成／再生成する",
            type="primary",
            key=f"coach_{selected_match_id}",
            disabled=not gemini_api_key,
        )
        if coach_clicked:
            try:
                if selected_match_id not in st.session_state.timelines_data:
                    timeline = get_match_timeline(riot_api_key, platform, selected_match_id)
                    st.session_state.timelines_data[selected_match_id] = build_timeline_summary(
                        timeline, selected_match, puuid
                    )
                timeline_events = st.session_state.timelines_data[selected_match_id]
                result_focus = (
                    "勝因と、さらに勝率を安定させる改善策"
                    if target_player.get("win")
                    else "敗因と、勝つために次回から実行できる改善策"
                )
                coaching_prompt = (
                    f"{DEFAULT_SYSTEM_PROMPT}\n\n"
                    "次の4つの見出しを、この順番で必ず使ってください。回答はすべて日本語にしてください。\n"
                    "### 【敗因/勝因のピンポイント解剖】\n"
                    "勝敗を分けた場面を時間軸から選び、何分ごろに確認できるイベントや判断が影響したかを説明してください。"
                    "時間や判断をデータから特定できない場合は、推測で断定せず、その限界を明記してください。\n"
                    "### 【ソロランク勝ちパターン診断】\n"
                    "レーニング、集団戦、サイドレーンの動きについて、今回の統計とイベントから強み・弱みを分けて説明してください。"
                    "1試合だけではプレイ傾向を断定できない場合は、その旨も添えてください。\n"
                    "### 【次回からすぐ試せる具体的アクション（宿題1選）】\n"
                    "宿題は必ず1つに絞り、実行するタイミング、確認すること、実際の行動を含む手順にしてください。"
                    "「気を付ける」のような曖昧な指示は禁止です。\n"
                    "### 【おすすめチャンピオン提案】\n"
                    "今回のポジションと確認できたプレイ内容に合う候補を1体提案し、理由を説明してください。"
                    "現在の環境や勝率をデータなしに断定せず、根拠が不足する場合はその旨を明記してください。\n"
                    f"今回の分析の重点：{result_focus}\n\n"
                    f"試合サマリー:\n{json.dumps(summary, ensure_ascii=False, indent=2)}\n\n"
                    "試合の時間軸イベント:\n"
                    f"{json.dumps(timeline_events, ensure_ascii=False, indent=2)}"
                )
                with st.spinner("AIコーチが試合を分析中..."):
                    coaching_result = st.write_stream(
                        make_gemini_response(gemini_api_key, coaching_prompt)
                    )
                if not coaching_result:
                    raise ValueError("AIコーチから回答がありませんでした。もう一度お試しください。")
                st.session_state.coaching_result = coaching_result
                st.session_state.coaching_match_id = selected_match_id
            except sqlite3.Error:
                st.error("試合の時間軸を読み込めませんでした。保存先を確認してください。")
            except ApiError as error:
                st.error(riot_error_message(error))
            except APIError:
                st.error("Gemini APIとの通信に失敗しました。時間をおいて再試行してください。")
            except RequestException:
                st.error("Gemini APIとの通信に失敗しました。接続を確認してください。")
            except ValueError as error:
                st.error(str(error))
        if st.session_state.coaching_result:
            st.markdown(st.session_state.coaching_result)
        else:
            st.info("ボタンを押すと、この試合の勝因・敗因と改善策をAIコーチが分析します。")

    with tabs[2]:
        if not gemini_api_key:
            st.error("AIコーチ機能は未設定です。運営者にGemini APIキーの設定を依頼してください。")
        for message in st.session_state.chat_history:
            with st.chat_message(
                message["role"],
                avatar=COACH_AVATAR if message["role"] == "assistant" else None,
            ):
                st.markdown(message["content"])
        user_message = st.chat_input(
            "例：なぜ集団戦に負けた？レーニングで意識することは？",
            disabled=not gemini_api_key,
        )
        if user_message:
            st.session_state.chat_history.append(
                {"role": "user", "content": user_message}
            )
            with st.chat_message("user"):
                st.markdown(user_message)
            history = "\n".join(
                f"{'ユーザー' if message['role'] == 'user' else 'AIコーチ'}: "
                f"{message['content']}"
                for message in st.session_state.chat_history[:-1]
            )
            chat_prompt = (
                f"{CHAT_SYSTEM_PROMPT}\n\n"
                "回答は日本語で、この試合のデータを根拠に質問へ具体的に答えてください。\n"
                f"試合サマリー:\n{json.dumps(summary, ensure_ascii=False, indent=2)}\n\n"
                f"これまでの会話:\n{history or '（なし）'}\n\n"
                f"ユーザー: {user_message}\nAIコーチ:"
            )
            try:
                with st.chat_message("assistant", avatar=COACH_AVATAR):
                    with st.spinner("AIコーチが回答を考えています..."):
                        answer = st.write_stream(
                            make_gemini_response(gemini_api_key, chat_prompt)
                        )
                if not answer:
                    raise ValueError("AIコーチから回答がありませんでした。もう一度お試しください。")
                st.session_state.chat_history.append(
                    {"role": "assistant", "content": answer}
                )
            except APIError:
                st.error("Gemini APIとの通信に失敗しました。時間をおいて再試行してください。")
            except ValueError as error:
                st.error(str(error))
else:
    st.markdown("### はじめかた")
    step_columns = st.columns(3)
    steps = [
        ("1. Riot IDを入力", "例：Player#JP1。名前の一部だけでも、同期済みのプレイヤーから候補を探せます。"),
        ("2. 戦績を同期", "キュータイプを選んで「最新の戦績を同期」を押すと、直近の試合を取得します。"),
        ("3. 試合を分析", "気になる試合で「この試合のコーチングを受ける」を押すと、AIコーチが改善策を提案します。"),
    ]
    for column, (title, body) in zip(step_columns, steps):
        with column:
            with st.container(border=True):
                st.markdown(f"**{title}**")
                st.caption(body)

st.divider()
st.caption(
    "LoL AI Coach は Riot Games の承認・公認を受けていません。Riot Games の見解を反映するものではありません。"
    "League of Legends と Riot Games は Riot Games, Inc. の商標または登録商標です。"
    "AIコーチの回答は参考情報です。プレイの判断はご自身で行ってください。"
)
