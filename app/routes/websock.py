# app/routes/websock.py

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Dict, List
import json
import logging
import asyncio
from app.db import fetch, fetchrow
from collections import defaultdict
from datetime import datetime, timedelta, timezone


router = APIRouter()
logger = logging.getLogger("websocket")

rooms: Dict[str, List[WebSocket]] = {}  # room_id => list of websockets
presence_by_room: Dict[str, Dict[str, str]] = {}  # room_id => {username: page or "offline"}
usernames_by_websocket: Dict[WebSocket, str] = {}
pending_disconnects: Dict[str, asyncio.Task] = {}  # room:username => task

# Round status tracking: "idle" | "started" | "paused"
round_flags = defaultdict(lambda: {"state": "idle", "start_at": None})

def everyone_ready(room_id: str) -> bool:
    presence = presence_by_room.get(room_id, {})
    return presence and all(page == "game_page" for page in presence.values())

async def connect_to_room(room: str, websocket: WebSocket):
    rooms.setdefault(room, []).append(websocket)

async def disconnect_from_room(room: str, websocket: WebSocket):
    if room not in rooms or websocket not in rooms[room]:
        return

    rooms[room].remove(websocket)
    username = usernames_by_websocket.pop(websocket, None)

    if username:
        key = f"{room}:{username}"

        # If user has no other socket connections, mark offline (after delay)
        async def delayed_offline():
            try:
                await asyncio.sleep(5)
                still_connected = any(
                    usernames_by_websocket.get(ws) == username
                    for ws in rooms.get(room, [])
                )
                if not still_connected:
                    presence_by_room[room][username] = "offline"
                    # logger.debug(f"[{room}] Marking {username} as offline after timeout")
                    await broadcast_presence(room, trigger_user=username, trigger_event="offline")
                pending_disconnects.pop(key, None)
            except asyncio.CancelledError:
                logger.debug(f"[{room}] Cancelled offline task for {username}")

        task = asyncio.create_task(delayed_offline())
        pending_disconnects[key] = task

    if not rooms[room]:
        rooms.pop(room)
        presence_by_room.pop(room, None)
        
async def broadcast(room: str, message: dict):
    if room not in rooms:
        return
    
    dead = []
    for ws in rooms[room]:
        try:
            if ws.client_state.name != "CONNECTED":
                raise RuntimeError("WebSocket not connected")
            await ws.send_text(json.dumps(message))
        except Exception as e:
            dead.append(ws)

    for ws in dead:
        await disconnect_from_room(room, ws)

async def broadcast_presence(room: str, trigger_user: str, trigger_event: str):
    session_id = int(room.split("_")[1])
    session_row = await fetchrow("SELECT players FROM sessions WHERE id = $1", session_id)
    max_players = session_row["players"]

    user_rows = await fetch("""
        SELECT users.username, session_users.is_host, user_scores.score, user_scores.winner
        FROM session_users
        JOIN users ON session_users.user_id = users.id
        LEFT JOIN user_scores ON session_users.user_id = user_scores.user_id AND session_users.session_id = user_scores.session_id
        WHERE session_users.session_id = $1
    """, session_id)

    players = [{"username": us["username"], "is_host": us["is_host"], "score": us["score"], "winner": us["winner"]} for us in user_rows]
    raw_presence = presence_by_room.get(room, {})
    presence = {u: page for u, page in raw_presence.items() if u}

    # Ensure all players have a presence state
    all_usernames = {u["username"] for u in user_rows}
    for name in all_usernames:
        presence.setdefault(name, "offline")

    # Determine current round number (highest round not yet ended)
    round_row = await fetchrow("""
        SELECT round FROM rounds
        WHERE session_id = $1 AND ended = FALSE
        ORDER BY round DESC LIMIT 1
    """, session_id)

    if round_row:
        # ✅ Ongoing round found
        current_round = round_row["round"]
    else:
        # 🟡 No active round — check if any rounds exist
        last_round_row = await fetchrow("""
            SELECT round FROM rounds
            WHERE session_id = $1
            ORDER BY round DESC LIMIT 1
        """, session_id)

        if last_round_row:
            current_round = last_round_row["round"]
        else:
            current_round = 1  # Brand new game

    # Determine round state
    flag = round_flags.get((session_id, current_round))

    if flag:
        round_state = flag["state"]
        round_start_at = flag["start_at"]
        round_end_at = flag["end_at"]
    else:
        # 🧼 Memory wiped OR first time round — fallback logic
        if round_row:  # An active round was just found, but no entry in memory
            round_state = "idle"
            round_start_at = None
            round_end_at = None
        elif last_round_row:
            # 🟠 All rounds ended, check for winner to show game over
            points_to_win_row = await fetchrow("SELECT points_to_win FROM sessions WHERE id = $1", session_id)
            winners_exist = await fetchrow("""
                SELECT * FROM user_scores
                WHERE session_id = $1 AND score = $2
            """, session_id, points_to_win_row["points_to_win"])
            if winners_exist:
                round_state = "game_over"
            else:
                round_state = "ended"  # fallback if game ended but no winner (shouldn't happen)
            round_start_at = None
            round_end_at = None
        else:
            round_state = "idle"
            round_start_at = None
            round_end_at = None

    print(f"({session_id}, {current_round}): State: {round_state}, Start: {round_start_at}, End: {round_end_at}")

    game_started = await fetchrow("SELECT * FROM game_started WHERE session_id = $1", session_id)
    is_paused = game_started["paused"] if game_started else False
    
    round_results = []
    round_winners = []

    if round_state == "results":
        round_results_raw = await fetch("""
            SELECT users.username, COUNT(*) as votes
            FROM votes
            JOIN users ON votes.voted_for_user_id = users.id
            WHERE votes.session_id = $1 AND votes.round = $2
            GROUP BY users.username
            ORDER BY votes DESC
        """, session_id, current_round)

        round_results = [{"username": row["username"], "votes": row["votes"]} for row in round_results_raw]
        if round_results:
            max_votes = round_results[0]["votes"]
            round_winners = [row["username"] for row in round_results if row["votes"] == max_votes]

    message = {
        "type": "session_update",
        "payload": {
            "session_id": session_id,
            "players": players,
            "presence": presence,
            "max_players": max_players,
            "is_paused": is_paused,
            "game_has_been_started": bool(game_started),
            "trigger_user": trigger_user,
            "trigger_event": trigger_event,
            "current_round": current_round,
            "round_state": round_state,
            "round_start_at": round_start_at.isoformat() if round_start_at else None,
            "round_end_at": round_end_at.isoformat() if round_end_at else None,
            "round_results": round_results,
            "round_winners": round_winners,
        }
    }
    # print(message)
    await broadcast(room, message)

@router.websocket("/ws/{room}")
async def websocket_endpoint(websocket: WebSocket, room: str):
    try:
        await websocket.accept()
        await connect_to_room(room, websocket)
    except Exception as e:
        return

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            if data["type"] == "presence_update":
                username = data.get("username", "").strip()
                page = data.get("page", "").strip()

                if not username:
                    logger.warning(f"[{room}] Ignoring presence update with empty username: {data}")
                    continue  # or optionally: await websocket.close(); return
                
                usernames_by_websocket[websocket] = username
                presence_by_room.setdefault(room, {})[username] = page

                key = f"{room}:{username}"
                if key in pending_disconnects:
                    pending_disconnects[key].cancel()
                    pending_disconnects.pop(key, None)
                
                await broadcast_presence(room, trigger_user=username, trigger_event="presence_update")

            else:
                await websocket.send_text(json.dumps({
                    "type": "echo",
                    "message": data
                }))

    except WebSocketDisconnect:
        await disconnect_from_room(room, websocket)
    except Exception as e:
        await disconnect_from_room(room, websocket)
