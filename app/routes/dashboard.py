import os
from fastapi import APIRouter, Request, Depends, Form, HTTPException, Query
from fastapi.responses import RedirectResponse, JSONResponse, Response
from urllib.parse import urlencode
from asyncio import gather
from openai import OpenAI
import httpx
from dotenv import load_dotenv
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from app.auth_utils import get_current_user, auth_required, split_sentences
from fastapi.templating import Jinja2Templates
from app.db import fetchrow, fetch, execute
from app.routes.websock import broadcast, broadcast_presence, presence_by_room, round_flags, everyone_ready


router = APIRouter()

templates = Jinja2Templates(directory="app/templates")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GIPHY_API_KEY = os.getenv("GIPHY_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

@router.get("/ping-time")
async def ping_time():
    return {"server_time": datetime.now(timezone.utc).isoformat()}

@router.get("/")
async def dashboard(request: Request, user: str = Depends(auth_required)):
    # Get user id from users table
    error_message = request.query_params.get("error")
    row = await fetchrow("""
        SELECT 
            s.*, 
            u2.username AS host_username,
            (
                SELECT COUNT(*) 
                FROM session_users 
                WHERE session_id = s.id
            ) AS user_count
        FROM sessions s
        JOIN session_users su ON su.session_id = s.id
        JOIN users u ON u.id = su.user_id
        JOIN users u2 ON u2.id = s.host_id
        WHERE u.username = $1 AND s.active = TRUE
        LIMIT 1
    """, user)

    if not row:
        return templates.TemplateResponse("dashboard.html", {
            "request": request, "user": user, "game_session": None, "error": error_message
        })
    
    active_session = dict(row)
    # print(active_session)
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "game_session": active_session,
        "error": error_message
    })

@router.get("/sessions")
async def sessions(request: Request, user: str = Depends(auth_required)):
    error_message = request.query_params.get("error")

    # Get user id from users table
    user_id_row = await fetchrow("SELECT id FROM users WHERE username = $1", user)
    user_id = user_id_row["id"]

    session_rows = await fetch("""
        SELECT
            s.*,
            u.username AS host_username,
            COUNT(su.user_id) AS user_count,
            BOOL_OR(su.user_id = $1) AS user_in
        FROM sessions s
        LEFT JOIN session_users su ON su.session_id = s.id
        LEFT JOIN users u ON u.id = s.host_id
        WHERE s.active = TRUE
        GROUP BY s.id, u.username
        ORDER BY s.id DESC
    """, user_id)

    sessions = [dict(row) for row in session_rows]

    return templates.TemplateResponse("sessions.html", {
        "request": request,
        "user": user,
        "sessions": sessions,
        "error": error_message
    })

@router.get("/create-session")
async def create_session(request: Request, user: str = Depends(auth_required)):
    return templates.TemplateResponse("create_session.html", {"request": request, "user": user})

@router.post("/create-session")
async def create_session_post(
    request: Request, 
    user: str = Depends(auth_required), 
    category: str = Form(...), 
    players: int = Form(...), 
    time_per_question: int = Form(...),
    points_to_win: int = Form(...)
):
    players = int(players)
    # Input validation
    if not category or players < 3 or players > 8 or time_per_question < 5 or time_per_question > 60 or points_to_win < 1 or points_to_win > 10:
        return templates.TemplateResponse("create_session.html", {
            "request": request,
            "user": user,
            "error": "Invalid game session parameters"
        }, status_code=400)
    try:
        # ✅ Fetch user_id AND check if they're in an active session concurrently
        user_id_row, user_in_active_session = await gather(
            fetchrow("SELECT id FROM users WHERE username = $1", user),
            fetchrow("""
                SELECT 1 FROM session_users
                JOIN sessions ON session_users.session_id = sessions.id
                WHERE session_users.user_id = (SELECT id FROM users WHERE username = $1)
                AND sessions.active = TRUE
            """, user)
        )

        user_id = user_id_row["id"]

        if user_in_active_session:
            return templates.TemplateResponse("create_session.html", {
                "request": request,
                "user": user,
                "error": "You are already in an active game session and cannot create a new one"
            }, status_code=400)

        # ✅ Do inserts in a chain — session → users → rounds
        session_result = await execute("""
            INSERT INTO sessions (category, players, time_per_question, points_to_win, host_id, active)
            VALUES ($1, $2, $3, $4, $5, TRUE)
            RETURNING id
        """, category, players, time_per_question, points_to_win, user_id)

        session_id = session_result[0]["id"]

        # ✅ Insert host as player and insert a score for the host
        await gather(
            execute("""
                INSERT INTO session_users (session_id, user_id, is_host)
                VALUES ($1, $2, TRUE)
            """, session_id, user_id),
            execute("""
                INSERT INTO user_scores (session_id, user_id, score)
                VALUES ($1, $2, 0)
            """, session_id, user_id)
        )

        # ✅ Broadcast only after the inserts complete
        await broadcast("sessions", {
            "type": "session_created",
            "session": {
                "id": session_id,
                "category": category,
                "max_players": players,
                "players_current": 1,
                "host_username": user,
                "time_per_question": time_per_question,
                "points_to_win": points_to_win
            }
        })

        return RedirectResponse(url=f"/host-lobby/{session_id}", status_code=302)

    except Exception as e:
        print(f"Error creating session: {e}")
        return templates.TemplateResponse("create_session.html", {
            "request": request,
            "user": user,
            "error": "An unexpected error occurred"
        }, status_code=500)

@router.post("/join/{session_id}")
async def join_session(session_id: int, request: Request, user: str = Depends(auth_required)):
    # Fetch user ID, session existence, and any active session concurrently
    session_details, user_id_row, active_session_row, user_in_session_row, current_count_row = await gather(
        fetchrow("SELECT * FROM sessions WHERE id = $1", session_id),
        fetchrow("SELECT id FROM users WHERE username = $1", user),
        fetchrow("""
            SELECT session_users.session_id
            FROM session_users
            JOIN sessions ON session_users.session_id = sessions.id
            WHERE session_users.user_id = (SELECT id FROM users WHERE username = $1)
              AND sessions.active = TRUE
        """, user),
        fetchrow("""
            SELECT 1 AS user_in_session FROM session_users WHERE session_id = $1 AND user_id = (SELECT id FROM users WHERE username = $2)
        """, session_id, user),
        fetchrow("""
            SELECT COUNT(*) AS count FROM session_users WHERE session_id = $1
        """, session_id)
    )

    if not session_details:
        return RedirectResponse(
            url=f"/sessions?{urlencode({'error': 'Session not found'})}", status_code=303
        )

    user_id = user_id_row["id"]

    # Block joining if user is in another active session
    if active_session_row and active_session_row["session_id"] != session_id:
        return RedirectResponse(
            url=f"/sessions?{urlencode({'error': 'You are already in another active session'})}", status_code=303
        )

    if not user_in_session_row:
        # Check current player count
        current_count = current_count_row["count"]

        if current_count >= session_details["players"]:
            return RedirectResponse(
                url=f"/sessions?{urlencode({'error': 'Max number of players for this session has been reached'})}",
                status_code=303
            )

        try:
            # Add user to the session
            await gather(
                execute("""
                    INSERT INTO session_users (session_id, user_id) VALUES ($1, $2)
                """, session_id, user_id),
                execute("""
                    INSERT INTO user_scores (session_id, user_id, score)
                    VALUES ($1, $2, 0)
                    ON CONFLICT (session_id, user_id) DO UPDATE SET score = 0
                """, session_id, user_id)
            )

            # Get updated player list
            updated_players_rows = await fetch("""
                SELECT users.username, session_users.is_host
                FROM session_users
                JOIN users ON users.id = session_users.user_id
                WHERE session_users.session_id = $1
            """, session_id)

            player_dicts = [
                {"username": row["username"], "is_host": row["is_host"]}
                for row in updated_players_rows
            ]
            updated_count = len(player_dicts)

            # Broadcast updated session info
            await gather(
                broadcast("sessions", {
                    "type": "session_update",
                    "payload": {
                        "player_count": {
                            "session_id": session_id,
                            "user_count": updated_count,
                            "max_players": session_details["players"]
                        }
                    }
                }),
                broadcast_presence(room=f"session_{session_id}", trigger_user=user, trigger_event="joined")
            )

        except Exception as e:
            print(f"Exception occurred: {e}")
            return templates.TemplateResponse("sessions.html", {
                "request": request,
                "user": user,
                "error": "An unexpected error occurred"
            }, status_code=500)

    return RedirectResponse(url=f"/waiting-area/{session_id}", status_code=302)

@router.get("/waiting-area/{session_id}")
async def waiting_area(session_id: int, request: Request, user: str = Depends(auth_required)):
    session_row, user_rows, game_started = await gather(
        fetchrow("SELECT * FROM sessions WHERE id = $1", session_id),
        fetch("""
            SELECT users.username, session_users.is_host
            FROM session_users
            JOIN users ON session_users.user_id = users.id
            WHERE session_users.session_id = $1
        """, session_id),
        fetchrow("SELECT * FROM game_started WHERE session_id = $1", session_id)
    )

    if not session_row:
        return templates.TemplateResponse("sessions.html", {
            "request": request,
            "user": user,
            "error": "Session not found"
        }, status_code=500)

    # Convert Record rows to JSON-serializable dicts
    user_dicts = [{"username": row["username"], "is_host": row["is_host"]} for row in user_rows]

    room_id = f"session_{session_id}"
    presence_state = presence_by_room.get(room_id, {})
    presence_state[user] = "waiting_area"
    # print(presence_state)

    is_paused = game_started["paused"] if game_started else False

    return templates.TemplateResponse("waiting_area.html", {
        "request": request,
        "user": user,
        "game_session": session_row,
        "users": user_dicts,
        "user_count": len(user_dicts),
        "presence": presence_state,
        "is_paused": is_paused,
        "game_has_been_started": True if game_started else False
    })

@router.post("/leave/{session_id}")
async def leave_session(session_id: int, request: Request, user: str = Depends(auth_required)):
    form = await request.form()
    next_url = form.get("next") or "/sessions"

    # Get session and user_id concurrently
    user_row, session_row = await gather(
        fetchrow("SELECT id FROM users WHERE username = $1", user),
        fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
    )

    if not session_row:
        params = urlencode({"error": "Session not found or already deleted"})
        return RedirectResponse(url=f"{next_url}?{params}", status_code=303)

    user_id = user_row["id"]

    try:
        # Remove user from session
        await gather(
            execute("DELETE FROM session_users WHERE session_id = $1 AND user_id = $2",session_id, user_id),
            execute("DELETE FROM user_scores WHERE session_id = $1 and user_id = $2", session_id, user_id)
        )

        # Fetch updated players
        updated_players = await fetch("""
            SELECT users.username, session_users.is_host
            FROM session_users
            JOIN users ON users.id = session_users.user_id
            WHERE session_users.session_id = $1
        """, session_id)

        player_dicts = [
            {"username": row["username"], "is_host": row["is_host"]}
            for row in updated_players
        ]

        updated_count = len(player_dicts)

        # Clean up in-memory presence for that user
        presence_by_room.get(f"session_{session_id}", {}).pop(user, None)
        await gather(
            broadcast("sessions", {
                "type": "session_update",
                "payload": {
                    "player_count": {
                        "session_id": session_id,
                        "user_count": updated_count,
                        "max_players": session_row["players"]
                    }
                }
            }),
            broadcast_presence(room=f"session_{session_id}", trigger_user=user, trigger_event="left")
        )

        return RedirectResponse(url=next_url, status_code=302)

    except Exception as e:
        print(f"[ERROR] Failed to remove {user} from session {session_id}: {e}")
        params = urlencode({"error": "Failed to leave session"})
        return RedirectResponse(url=f"{next_url}?{params}", status_code=303)
    
@router.post("/delete/{session_id}")
async def delete_session(session_id: int, request: Request, user: str = Depends(auth_required)):
    form = await request.form()
    next_url = form.get("next") or "/sessions"

    # Fetch user_id and session concurrently
    user_row, session = await gather(
        fetchrow("SELECT id FROM users WHERE username = $1", user),
        fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)
    )

    if not session:
        params = urlencode({"error": "Session not found or already deleted"})
        return RedirectResponse(url=f"{next_url}?{params}", status_code=303)
    
    user_id = user_row["id"]

    if session["host_id"] != user_id:
        params = urlencode({"error": "Only the host can delete this session"})
        return RedirectResponse(url=f"{next_url}?{params}", status_code=303)

    try:
        # Check player count quickly
        count_row = await fetchrow(
            "SELECT COUNT(*) AS count FROM session_users WHERE session_id = $1", session_id
        )
        if count_row["count"] > 1:
            params = urlencode({"error": "Cannot delete session with multiple users"})
            return RedirectResponse(url=f"{next_url}?{params}", status_code=303)

        await execute("DELETE FROM game_sentences WHERE session_id = $1", session_id)
        await execute("DELETE FROM session_users WHERE session_id = $1", session_id)
        await execute("DELETE FROM rounds WHERE session_id = $1", session_id)
        await execute("DELETE FROM sessions WHERE id = $1", session_id)


        await broadcast("sessions", {
            "type": "session_deleted",
            "session_id": session_id
        })
        
        return RedirectResponse(url=next_url, status_code=302)

    except Exception as e:
        print(f"Error deleting session: {e}")
        params = urlencode({"error": "Failed to delete session"})
        redirect_url = f"{next_url}?{params}"
        return RedirectResponse(url=redirect_url, status_code=303)    

@router.get("/host-lobby/{session_id}")
async def host_lobby(session_id: int, request: Request, user: str = Depends(auth_required)):
    error_message = request.query_params.get("error")

    user_id_row, session_details, game_started = await gather(
        fetchrow("SELECT id FROM users WHERE username = $1", user),
        fetchrow("SELECT * FROM sessions WHERE id = $1", session_id),
        fetchrow("SELECT * FROM game_started WHERE session_id = $1", session_id)
    )

    if not session_details:
        params = urlencode({"error": "Session not found or already deleted"})
        return RedirectResponse(url=f"/sessions?{params}", status_code=303)
    
    user_id = user_id_row["id"]
    
    # Combine active session and user_in_session check in one query
    active_and_user_session = await fetchrow("""
        SELECT 
            (SELECT session_id FROM session_users 
             JOIN sessions ON session_users.session_id = sessions.id
             WHERE session_users.user_id = $1 AND sessions.active = TRUE LIMIT 1) AS active_session_id,
            (SELECT 1 FROM session_users WHERE session_id = $2 AND user_id = $1 LIMIT 1) AS user_in_session
    """, user_id, session_id)

    active_session_id = active_and_user_session["active_session_id"]
    user_in_session = active_and_user_session["user_in_session"]

    if active_session_id and active_session_id != session_id:
        params = urlencode({"error": "You are already in another active session"})
        return RedirectResponse(url=f"/sessions?{params}", status_code=303)

    is_host = session_details["host_id"] == user_id

    if not is_host or not user_in_session:
        params = urlencode({"error": "You do not have access to host controls for this session"})
        return RedirectResponse(url=f"/sessions?{params}", status_code=303)

    # Fetch users in the session
    users_in_session = await fetch("""
        SELECT users.username, session_users.is_host
        FROM session_users
        JOIN users ON session_users.user_id = users.id
        WHERE session_users.session_id = $1
    """, session_id)

    user_count = len(users_in_session)

    room_id = f"session_{session_id}"
    presence_state = presence_by_room.get(room_id, {})
    presence_state[user] = "host_lobby"
    # print(presence_state)

    # Make sure all usernames are present with at least "offline"
    for player in users_in_session:
        username = player["username"]
        presence_state.setdefault(username, "offline")

    is_paused = game_started["paused"] if game_started else False

    return templates.TemplateResponse("host_lobby.html", {
        "request": request,
        "user": user,
        "error": error_message,
        "game_session": session_details,
        "users": users_in_session,
        "user_count": user_count,
        "presence": presence_state,
        "is_paused": is_paused,
        "game_has_been_started": True if game_started else False
    })

@router.post("/submit-changes/{session_id}")
async def submit_changes(session_id: int, request: Request, user: str = Depends(auth_required)):
    form_data = await request.form()

    category = form_data.get("category")
    players = int(form_data.get("players"))
    time_per_question = int(form_data.get("time_per_question"))
    points_to_win = int(form_data.get("points_to_win"))

    user_row, session, user_count_row, sentence_check = await gather(
        fetchrow("SELECT id FROM users WHERE username = $1", user),
        fetchrow("SELECT * FROM sessions WHERE id = $1", session_id),
        fetchrow("SELECT COUNT(*) AS count FROM session_users WHERE session_id = $1", session_id),
        fetchrow("SELECT COUNT(*) AS count FROM game_sentences WHERE session_id = $1", session_id)
    )

    if not session:
        params = urlencode({"error": "Session not found or already deleted"})
        return RedirectResponse(url=f"/sessions?{params}", status_code=303)

    user_id = user_row["id"]
    if session["host_id"] != user_id:
        params = urlencode({"error": "Only the host can edit the session"})
        return RedirectResponse(url=f"/sessions?{params}", status_code=303)

    if sentence_check["count"] > 0:
        params = urlencode({
            "error": "Cannot edit session settings after the game has started."
        })
        return RedirectResponse(url=f"/host-lobby/{session_id}?{params}", status_code=303)

    user_count = user_count_row["count"]
    if players < user_count:
        params = urlencode({
            "error": f"Cannot reduce player count to {players}, because {user_count} player(s) already joined."})
        return RedirectResponse(url=f"/host-lobby/{session_id}?{params}", status_code=303)

    # Check if settings actually changed
    if (session["category"] == category and
        session["players"] == players and
        session["time_per_question"] == time_per_question and
        session["points_to_win"] == points_to_win):
        params = urlencode({"info": "Session settings are already up to date."})
        return RedirectResponse(url=f"/host-lobby/{session_id}?{params}", status_code=303)
    
    await execute("""
        UPDATE sessions 
        SET category = $1, players = $2, time_per_question = $3, points_to_win = $4
        WHERE id = $5
    """, category, players, time_per_question, points_to_win, session_id)

    # Broadcast updates
    await gather(
        broadcast("sessions", {
            "type": "session_details_updated",
            "session_id": session_id,
            "user_count": user_count,
            "new_category": category,
            "new_max_players": players
        }),
        broadcast(f"session_{session_id}", {
            "type": "session_details_updated",
            "session_id": session_id,
            "user_count": user_count,
            "new_category": category,
            "new_max_players": players,
            "new_time_per_question": time_per_question,
            "new_points_to_win": points_to_win
        })
    )

    return RedirectResponse(url=f"/host-lobby/{session_id}", status_code=303)

@router.get("/history")
async def history(request: Request, user: str = Depends(auth_required)):
    user_row = await fetchrow("SELECT id FROM users WHERE username = $1", user)
    user_id = user_row["id"]

    session_data = await fetch("""
        SELECT 
            s.*, 
            u.username AS winner_username,
            us.session_id AS score_session_id
        FROM sessions s
        JOIN session_users su ON su.session_id = s.id
        LEFT JOIN user_scores us ON us.session_id = s.id AND us.winner = TRUE
        LEFT JOIN users u ON u.id = us.user_id
        WHERE s.active = FALSE AND su.user_id = $1
        ORDER BY s.id DESC
    """, user_id)

    if not session_data:
        return templates.TemplateResponse("history.html", {
            "request": request,
            "user": user,
            "old_game_sessions": []
        })
    
    sessions_map = {}
    winners_map = defaultdict(list)

    for row in session_data:
        session_id = row["id"]
        
        # Add session info once
        if session_id not in sessions_map:
            sessions_map[session_id] = {
                "id": row["id"],
                "category": row["category"],
                "players": row["players"],
                "time_per_question": row["time_per_question"],
                "points_to_win": row["points_to_win"],
                "host_id": row["host_id"],
                "active": row["active"]
            }

        # Collect winners
        if row["winner_username"]:
            winners_map[session_id].append(row["winner_username"])

    # Combine sessions with their winners
    structured_sessions = []
    for session_id, session_info in sessions_map.items():
        session_info["winners"] = winners_map[session_id]
        structured_sessions.append(session_info)
    
    # print(structured_sessions)
    return templates.TemplateResponse("history.html", {
        "request": request,
        "user": user,
        "old_game_sessions": structured_sessions
    })

@router.post("/start-game/{session_id}")
async def start_game(session_id: int, user: str = Depends(auth_required)):
    user_row, session, players = await gather(
        fetchrow("SELECT id FROM users WHERE username = $1", user),
        fetchrow("SELECT * FROM sessions WHERE id = $1", session_id),
        fetch("""
            SELECT users.id AS user_id, users.username, session_users.is_host
            FROM session_users
            JOIN users ON users.id = session_users.user_id
            WHERE session_users.session_id = $1
        """, session_id),
    )
    if not session:
        params = urlencode({"error": "Session not found or already deleted"})
        return RedirectResponse(url=f"/sessions?{params}", status_code=303)

    if session["host_id"] != user_row["id"]:
        params = urlencode({"error": "Only the host can start the game"})
        return RedirectResponse(url=f"/sessions?{params}", status_code=303)

    # Validate presence
    room_id = f"session_{session_id}"
    presence_state = presence_by_room.get(room_id, {})
    missing_players = []

    for player in players:
        expected_page = "host_lobby" if player["is_host"] else "waiting_area"
        actual_page = presence_state.get(player["username"])
        if actual_page != expected_page:
            missing_players.append(f"{player['username']} (expected: {expected_page}, got: {actual_page or 'offline'})")

    if missing_players:
        return JSONResponse(
            status_code=400,
            content={
                "detail": "Cannot start game. Some players are not connected correctly.",
                "players": missing_players
            }
        )
    
    started_game = await fetchrow("SELECT * FROM game_started WHERE session_id = $1", session_id)
    print(started_game)

    if not started_game:
        try:
            await execute("DELETE FROM gif_urls WHERE session_id = $1", session_id)
            await execute("DELETE FROM rounds WHERE session_id = $1", session_id)

            await execute("""
                INSERT INTO game_started (session_id, started, paused)
                VALUES ($1, TRUE, FALSE)
            """, session_id)

            await execute("INSERT INTO rounds (session_id, round) VALUES ($1, 1)", session_id)

        except Exception as e:
            print("❌ Error during initial game start DB setup:", e)
            return JSONResponse(status_code=500, content={"detail": "Failed to initialize game"})

        current_round = 1

    elif started_game["paused"]:
        # Game is paused — resume from last round
        last_round = await fetchrow("""
            SELECT round FROM rounds
            WHERE session_id = $1 AND started = TRUE AND ended IS DISTINCT FROM TRUE
            ORDER BY round DESC LIMIT 1
        """, session_id)
        current_round = last_round["round"] if last_round else 1

        await execute("UPDATE game_started SET paused = FALSE WHERE session_id = $1", session_id)

    else:
        return JSONResponse(status_code=400, content={"detail": "Game already running."})

    # Calculate required statement count
    count_row = await fetchrow("SELECT COUNT(*) AS count FROM game_sentences WHERE session_id = $1", session_id)
    sentence_count = count_row["count"]
    points_to_win = session["points_to_win"]
    num_players = session["players"]
    need_to_generate = True

    if sentence_count == 0:
        required = num_players * (points_to_win - 1) + 1
    else:
        row = await fetchrow("""
            SELECT score, COUNT(*) AS frequency
            FROM user_scores
            WHERE session_id = $1
            GROUP BY score
            ORDER BY score ASC
            LIMIT 1
        """, session_id)
        lowest_score = row["score"] if row else 0
        frequency_of_lowest = row["frequency"] if row else 0

        points_for_lowest_to_win = points_to_win - lowest_score
        sentences_left = sentence_count - (current_round - 1)
        sentences_still_required = frequency_of_lowest * (points_for_lowest_to_win - 1) + 1
        if sentences_left < sentences_still_required:
            required = sentences_still_required - sentences_left
        else:
            need_to_generate = False

    if need_to_generate:
        # Generate statements with OpenAI
        try:
            category = session["category"]
            prompt = f"Give me {required} statements about {category} for a GIF reaction game."
            completion = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are playing a GIF reaction game where users search for a GIF that best describes a statement."},
                    {"role": "user", "content": prompt}
                ]
            )

            response_text = completion.choices[0].message.content.strip()
            sentences = split_sentences(response_text)
            cleaned = [s.strip('. "').strip() for s in sentences if s.strip()]

            await gather(*[
                execute("INSERT INTO game_sentences (session_id, sentence) VALUES ($1, $2)", session_id, sentence)
                for sentence in cleaned
            ])

        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": f"Failed to generate statements: {str(e)}"})

    countdown_seconds = 5
    start_at = datetime.now(timezone.utc) + timedelta(seconds=countdown_seconds)
    
    await broadcast(f"session_{session_id}", {
        "type": "start_game",
        "session_id": session_id,
        "round": current_round,
        "start_at": start_at.isoformat()
    })

    return Response(status_code=204)

@router.get("/game/{session_id}")
async def game_page(request: Request, session_id: int, user: str = Depends(auth_required)):
    user_row, session, game_started, is_host_row = await gather(
        fetchrow("SELECT id FROM users WHERE username = $1", user),
        fetchrow("SELECT * FROM sessions WHERE id = $1", session_id),
        fetchrow("SELECT * FROM game_started WHERE session_id = $1", session_id),
        fetchrow("""
            SELECT is_host FROM session_users 
            JOIN users ON users.id = session_users.user_id
            WHERE users.username = $1 AND session_users.session_id = $2
        """, user, session_id)
    )

    if not session or not is_host_row:
        params = urlencode({"error": "Session not found"})
        return RedirectResponse(url=f"/sessions?{params}", status_code=303)
    
    if not game_started:
        page = "host-lobby" if is_host_row["is_host"] else "waiting-area"
        return RedirectResponse(f"/{page}/{session_id}?error=Game has not started!", status_code=303)
    if game_started["paused"]:
        page = "host-lobby" if is_host_row["is_host"] else "waiting-area"
        return RedirectResponse(f"/{page}/{session_id}?error=Game is currently paused!", status_code=303)
    
    latest_round = await fetchrow("""
            SELECT round FROM rounds 
            WHERE session_id = $1 AND started = TRUE AND ended = FALSE
            ORDER BY round DESC LIMIT 1
        """, session_id)
    if latest_round:
        current_round = latest_round["round"]
    else:
        last_round_row = await fetchrow("""
            SELECT round FROM rounds
            WHERE session_id = $1
            ORDER BY round DESC LIMIT 1
        """, session_id)
        current_round = last_round_row["round"]

    users_in_session, game_sentences_raw, submitted_gifs_raw, votes_cast_row, user_voted_row, winners_row = await gather(
        fetch("""
            SELECT users.username, session_users.is_host, user_scores.score, user_scores.winner
            FROM session_users
            JOIN users ON session_users.user_id = users.id
            LEFT JOIN user_scores 
              ON session_users.user_id = user_scores.user_id 
             AND session_users.session_id = user_scores.session_id
            WHERE session_users.session_id = $1
        """, session_id),
        fetch("SELECT sentence FROM game_sentences WHERE session_id = $1", session_id),
        fetch("""
            SELECT u.username, g.gif_url, g.is_n 
            FROM gif_urls g
            JOIN users u ON g.user_id = u.id
            WHERE g.session_id = $1 AND g.round = $2
        """, session_id, current_round),
        fetchrow("SELECT COUNT(*) AS count FROM votes WHERE session_id = $1 AND round = $2", session_id, current_round),
        fetchrow("""
            SELECT 1 FROM votes
            JOIN users ON votes.user_id = users.id
            WHERE votes.session_id = $1 AND votes.round = $2 AND users.username = $3
        """, session_id, current_round, user),
        fetch("""
            SELECT username AS winners
            FROM users
            JOIN user_scores ON users.id = user_scores.user_id
            WHERE user_scores.session_id = $1 AND user_scores.score = $2
        """, session_id, session["points_to_win"])
    )
    
    game_sentences = [row["sentence"] for row in game_sentences_raw]
    current_sentence = game_sentences[current_round - 1] if current_round - 1 < len(game_sentences) else "Statement unavailable"

    submitted_gifs = [{"username": r["username"], "gif_url": r["gif_url"], "is_null": r["is_n"]} for r in submitted_gifs_raw]
    submitted_usernames = {row["username"] for row in submitted_gifs}
    all_usernames = {player["username"] for player in users_in_session}

    user_has_submitted = user in submitted_usernames
    user_has_voted = bool(user_voted_row)
    votes_cast = votes_cast_row["count"]
    total_players = len(users_in_session)
    all_votes_submitted = votes_cast == total_players
    all_gifs_submitted = submitted_usernames == all_usernames
    
    room_id = f"session_{session_id}"
    presence_state = presence_by_room.setdefault(room_id, {})
    presence_state[user] = "game_page"
    for player in users_in_session:
        presence_state.setdefault(player["username"], "offline")

    flag = round_flags.get((session_id, current_round), {"state": "idle", "start_at": None, "end_at": None})
    print(flag)
    round_state = flag["state"]
    round_start_at = flag["start_at"] if flag["start_at"] else None
    round_end_at = flag["end_at"] if flag["end_at"] else None

    round_results = []
    round_winners = []
    winners = []
    leaderboard = []

    if winners_row:
        winners = [row["winners"] for row in winners_row]
        leaderboard_rows = await fetch("""
            SELECT users.username, user_scores.score
            FROM users
            JOIN user_scores ON users.id = user_scores.user_id
            WHERE user_scores.session_id = $1
        """, session_id)
        leaderboard = [{"username": row["username"], "score": row["score"]} for row in leaderboard_rows]
        round_state = "game_over"
    elif all_votes_submitted:
        round_state = "results"

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
    elif all_gifs_submitted:
        round_state = "voting"
    
    round_flags[(session_id, current_round)] = {
        "state": round_state,
        "start_at": round_start_at,
        "end_at": round_end_at
    }

    return templates.TemplateResponse("game.html", {
        "request": request,
        "session_id": session_id,
        "round": current_round,
        "user": user,
        "is_host": is_host_row["is_host"],
        "time_per_question": session["time_per_question"],
        "users": users_in_session,
        "user_count": total_players,
        "presence": presence_state,
        "current_sentence": current_sentence,
        "round_state": round_state,
        "round_start_at": round_start_at,
        "round_end_at": round_end_at,
        "all_gifs_submitted": all_gifs_submitted,
        "submitted_gifs": submitted_gifs,
        "user_has_submitted": user_has_submitted,
        "user_has_voted": user_has_voted,
        "votes_cast": votes_cast,
        "all_votes_submitted": all_votes_submitted,
        "round_results": round_results,
        "round_winners": round_winners,
        "winners": winners,
        "leaderboard": leaderboard
    })

@router.post("/pause-game/{session_id}")
async def pause_game(session_id: int, user: str = Depends(auth_required)):
    user_row = await fetchrow("SELECT id FROM users WHERE username = $1", user)
    session = await fetchrow("SELECT * FROM sessions WHERE id = $1", session_id)

    if not session["active"]:
        return JSONResponse(status_code=403, content={"detail": "The session is no longer active."})
    
    if session["host_id"] != user_row["id"]:
        return JSONResponse(status_code=403, content={"detail": "Only the host can pause the game."})
    
    round_row = await fetchrow("""
            SELECT round FROM rounds
            WHERE session_id = $1 AND started = TRUE AND ended IS DISTINCT FROM TRUE
            ORDER BY round DESC LIMIT 1
        """, session_id)
    if round_row:
        current_round = round_row["round"]
    else:
        last_round_row = await fetchrow("""
            SELECT round FROM rounds
            WHERE session_id = $1
            ORDER BY round DESC LIMIT 1
        """, session_id)

        if last_round_row:
            current_round = last_round_row["round"]
        else:
            current_round = 1  # Brand new game

    flag = round_flags.get((session_id, current_round))
    if flag:
        round_state = flag["state"]
    else:
        if round_row:
            round_state = "idle"
        else:
            winners_exist = await fetchrow("""
                SELECT COUNT(*) FROM user_scores
                WHERE session_id = $1 AND score = $2
            """, session_id, session["points_to_win"])
            
            if winners_exist:
                round_state = "game_over"
            else:
                round_state = "ended"
    
    if round_state in {"idle", "new_round", "results", "ended", "game_over"}:
        return JSONResponse(status_code=403, content={"detail": f"The session can't be paused due to its state: {round_state}"})

    await execute("UPDATE game_started SET paused = TRUE WHERE session_id = $1", session_id)
    await execute ("UPDATE rounds SET started = FALSE, paused = FALSE WHERE session_id = $1 AND round = $2", session_id, current_round)

    await execute("DELETE FROM gif_urls WHERE session_id = $1 AND round = $2", session_id, current_round)
    await execute("DELETE FROM votes WHERE session_id = $1 AND round = $2", session_id, current_round)

    round_flags[(session_id, current_round)] = {
        "state": "idle",
        "start_at": None,
        "end_at": None
    }

    # Broadcast pause countdown
    countdown_seconds = 5
    pause_at = datetime.now(timezone.utc) + timedelta(seconds=countdown_seconds)
    await broadcast(f"session_{session_id}", {
        "type": "game_paused",
        "session_id": session_id,
        "pause_at": pause_at.isoformat()
    })

    return JSONResponse({"status": "paused"})

@router.post("/start-round/{session_id}/{round}")
async def start_round(session_id: int, round: int, user: str = Depends(auth_required)):
    room_id = f"session_{session_id}"

    if not everyone_ready(room_id):
        return Response(status_code=409)
    
    round_row, session_row = await gather(
        fetchrow("SELECT * FROM rounds WHERE session_id = $1 AND round = $2", session_id, round),
        fetchrow("SELECT time_per_question FROM sessions WHERE id = $1", session_id)
    )
    if not round_row or not session_row:
        return Response(status_code=404)

    time_per_question = session_row["time_per_question"]
    now = datetime.now(timezone.utc)

    if not round_row["started"]:
        countdown_seconds = 5
        start_at = now + timedelta(seconds=countdown_seconds)
        end_at = start_at + timedelta(seconds=time_per_question)

        await execute("""
            UPDATE rounds
            SET started = TRUE, paused = FALSE, start_at = $1, end_at = $2
            WHERE session_id = $3 AND round = $4
        """, start_at, end_at, session_id, round)

        round_flags[(session_id, round)] = {
            "state": "started",
            "start_at": start_at,
            "end_at": end_at
        }

        await broadcast(room_id, {
            "type": "start_round",
            "session_id": session_id,
            "round": round,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat()
        })
        return Response(status_code=204)
    
    elif round_row["paused"]:
        pause_at = round_row["pause_at"]
        end_at = round_row["end_at"]
        if not pause_at or not end_at:
            return Response(status_code=500)

        remaining = (end_at - pause_at).total_seconds()
        if remaining <= 0:
            return Response(status_code=400, content={"detail": "Round already expired"})

        resume_at = now + timedelta(seconds=5)
        new_end_at = resume_at + timedelta(seconds=remaining)

        await execute("""
            UPDATE rounds
            SET paused = FALSE, resume_at = $1, end_at = $2
            WHERE session_id = $3 AND round = $4
        """, resume_at, new_end_at, session_id, round)

        round_flags[(session_id, round)] = {
            "state": "started",
            "start_at": resume_at,
            "end_at": new_end_at
        }

        await broadcast(room_id, {
            "type": "resume_round",
            "session_id": session_id,
            "round": round,
            "start_at": resume_at.isoformat(),
            "end_at": new_end_at.isoformat()
        })
        return Response(status_code=204)

    # Already started and running
    return Response(status_code=204)
    

@router.post("/pause-round/{session_id}/{round}")
async def pause_round(session_id: int, round: int, user: str = Depends(auth_required)):
    room_id = f"session_{session_id}"

    if everyone_ready(room_id):
        return Response(status_code=204)
    
    round_row = await fetchrow("SELECT * FROM rounds WHERE session_id = $1 AND round = $2", session_id, round)
    if not round_row:
        return Response(status_code=404)

    if round_row["paused"]:
        return Response(status_code=204)

    now = datetime.now(timezone.utc)

    await execute("""
        UPDATE rounds
        SET paused = TRUE, pause_at = $1
        WHERE session_id = $2 AND round = $3
    """, now, session_id, round)

    round_flags[(session_id, round)] = {
        "state": "paused",
        "start_at": None,
        "end_at": None
    }

    await broadcast(room_id, {
        "type": "pause_round",
        "session_id": session_id,
        "round": round
    })
    return Response(status_code=204)

@router.get("/search-gifs")
async def search_gifs(query: str = Query(...), user: str = Depends(auth_required)):
    async with httpx.AsyncClient() as client:
        response = await client.get("https://api.giphy.com/v1/gifs/search", params={
            "api_key": GIPHY_API_KEY,
            "q": query,
            "limit": 25
        })
    gifs = response.json().get("data", [])
    return JSONResponse(content={"gifs": gifs})

@router.post("/save-gif/{session_id}/{round}")
async def save_gif(session_id: int, round: int, selected_gif: str = Form(None), request: Request = None, user: dict = Depends(auth_required)):
    user_row, existing = await gather(
        fetchrow("SELECT id FROM users WHERE username = $1", user),
        fetchrow("SELECT 1 FROM gif_urls WHERE session_id = $1 AND user_id = (SELECT id FROM users WHERE username = $2) AND round = $3", session_id, user, round)
    )
    user_id = user_row["id"]

    if existing:
        return JSONResponse({"status": "already_submitted"}, status_code=200)
    
    await execute(
        "INSERT INTO gif_urls (session_id, user_id, gif_url, round, is_n) VALUES ($1, $2, $3, $4, $5)",
        session_id, user_id, selected_gif, round, selected_gif is None
    )

    # Fetch all current submissions
    submissions_raw, total_players_row = await gather(
        fetch("""
            SELECT users.id AS user_id, users.username, gif_urls.gif_url, gif_urls.is_n
            FROM gif_urls
            JOIN users ON gif_urls.user_id = users.id
            WHERE gif_urls.session_id = $1 AND gif_urls.round = $2
        """, session_id, round),
        fetchrow("""
            SELECT COUNT(*) AS count FROM session_users
            WHERE session_id = $1
        """, session_id)
    )

    submissions = [{"user_id": r["user_id"], "username": r["username"], "gif_url": r["gif_url"], "is_null": r["is_n"]} for r in submissions_raw]
    total_players = total_players_row["count"]
    all_submitted = len(submissions) == total_players

    public_submissions = [
        {"username": r["username"], "gif_url": r["gif_url"], "is_null": r["is_null"]}
        for r in submissions if not r["is_null"]
    ]
    # Broadcast updated submissions
    await broadcast(f"session_{session_id}", {
        "type": "gif_submissions",
        "submissions": public_submissions
    })

    flag = round_flags.get((session_id, round), {})
    if all_submitted and flag.get("state") not in {"voting", "results", "ended"}:
        # Everyone has submitted → start voting
        non_null_submissions = [r for r in submissions if not r["is_null"]]
        user_ids = [r["user_id"] for r in submissions]

        existing_votes = await fetch("""
            SELECT user_id FROM votes
            WHERE session_id = $1 AND round = $2
        """, session_id, round)
        already_voted = {row["user_id"] for row in existing_votes}
    
        round_results = []
        round_winners = []

        if len(non_null_submissions) == 1:
            # Only one real gif → give that player a point
            sole_user_id = non_null_submissions[0]["user_id"]
            sole_username = non_null_submissions[0]["username"]

            await gather(*[
                execute("""
                    INSERT INTO votes (session_id, round, user_id, voted_for_user_id)
                    VALUES ($1, $2, $3, $4)
                """, session_id, round, uid, sole_user_id)
                for uid in user_ids if uid not in already_voted
            ])

            await execute("""
                UPDATE user_scores
                SET score = score + 1
                WHERE session_id = $1 AND user_id = $2
            """, session_id, sole_user_id)

            round_results = [{"username": sole_username, "votes": total_players}]
            round_winners = [sole_username]

            round_flags[(session_id, round)]["state"] = "results"
            await broadcast(f"session_{session_id}", {
                "type": "results",
                "round_winners": round_winners,
                "round_results": round_results
            })
        
        elif len(non_null_submissions) == 0:
            await gather(*[
                execute("""
                    INSERT INTO votes (session_id, round, user_id, voted_for_user_id)
                    VALUES ($1, $2, $3, $4)
                """, session_id, round, uid, uid)
                for uid in user_ids if uid not in already_voted
            ])
            await gather(*[
                execute("""
                    UPDATE user_scores
                    SET score = score + 1
                    WHERE session_id = $1 AND user_id = $2
                """, session_id, uid)
                for uid in user_ids
            ])
            usernames = [r["username"] for r in submissions]
            round_results = [{"username": u, "votes": 1} for u in usernames]
            round_winners = usernames

            round_flags[(session_id, round)]["state"] = "results"
            await broadcast(f"session_{session_id}", {
                "type": "results",
                "round_winners": round_winners,
                "round_results": round_results
            })

        else:
            round_flags[(session_id, round)]["state"] = "voting"
            await broadcast(f"session_{session_id}", {
                "type": "start_voting",
                "round": round
            })

    return JSONResponse({"status": "success", "submissions": public_submissions, "all_submitted": all_submitted}, status_code=200)

@router.post("/vote/{session_id}/{round}")
async def vote(session_id: int, round: int, voted_for_user: str = Form(...), user: dict = Depends(auth_required)):
    voter, voted = await gather(
        fetchrow("SELECT id FROM users WHERE username = $1", user),
        fetchrow("SELECT id FROM users WHERE username = $1", voted_for_user)
    )

    if not voter or not voted:
        return JSONResponse({"status": "error", "message": "Invalid users"}, status_code=400)
    
    voter_id, voted_id = voter["id"], voted["id"]

    # Check if already voted
    existing = await fetchrow("""
        SELECT 1 FROM votes
        WHERE session_id = $1 AND round = $2 AND user_id = $3
    """, session_id, round, voter_id)
    if existing:
        return JSONResponse({"status": "already_voted"}, status_code=200)

    # Save vote
    await execute("""
        INSERT INTO votes (session_id, round, user_id, voted_for_user_id)
        VALUES ($1, $2, $3, $4)
    """, session_id, round, voter_id, voted_id)

    votes_cast_row, total_players_row = await gather(
        fetchrow("""
            SELECT COUNT(*) AS count FROM votes
            WHERE session_id = $1 AND round = $2
        """, session_id, round),
        fetchrow("""
            SELECT COUNT(*) AS count FROM session_users
            WHERE session_id = $1
        """, session_id)
    )

    votes_cast = votes_cast_row["count"]
    total_players = total_players_row["count"]
    all_voted = votes_cast == total_players

    current_flag = round_flags.get((session_id, round), {})
    if current_flag.get("state") in {"results", "ended", "game_over"}:
        return JSONResponse({"status": "success", "all_voted": all_voted})
    
    round_results = []
    round_winners = []
    # 🔧 If all voted, tally results
    if all_voted and current_flag.get("state") not in {"results", "ended", "game_over"}:
        round_results_raw = await fetch("""
            SELECT users.username, COUNT(*) as votes
            FROM votes
            JOIN users ON votes.voted_for_user_id = users.id
            WHERE votes.session_id = $1 AND votes.round = $2
            GROUP BY users.username
            ORDER BY votes DESC
        """, session_id, round)

        round_results = [{"username": row["username"], "votes": row["votes"]} for row in round_results_raw]
        max_votes = round_results[0]["votes"] if round_results else 0
        round_winners = [r["username"] for r in round_results if r["votes"] == max_votes]

        await gather(*[
            execute("""
                UPDATE user_scores
                SET score = score + 1
                WHERE session_id = $1 AND user_id = (SELECT id FROM users WHERE username = $2)
            """, session_id, username)
            for username in round_winners
        ])

        # 🔧 Update round state to "results"
        round_flags[(session_id, round)]["state"] = "results"
        await broadcast(f"session_{session_id}", {
            "type": "results",
            "round_winners": round_winners,
            "round_results": round_results
        })

    return JSONResponse({"status": "success", "all_voted": all_voted, "round_results": round_results, "round_winners": round_winners})

@router.post("/next-round/{session_id}/{round}")
async def next_round(session_id: int, round: int, user: str = Depends(auth_required)):
    is_host_row = await fetchrow("""
        SELECT is_host FROM session_users 
        JOIN users ON users.id = session_users.user_id
        WHERE users.username = $1 AND session_users.session_id = $2
    """, user, session_id)
    if not is_host_row or not is_host_row["is_host"]:
        return JSONResponse({"error": "Only the host can start next round"}, status_code=403)

    flag = round_flags.get((session_id, round))
    if not flag or flag.get("state") != "results":
        return JSONResponse({"error": "Current round not in results state"}, status_code=400)

    await execute("""
        UPDATE rounds SET ended = TRUE, paused = FALSE WHERE session_id = $1 AND round = $2
    """, session_id, round)
    round_flags[(session_id, round)] = {
        "state": "ended",
        "start_at": None,
        "end_at": None
    }

    session = await fetchrow("SELECT points_to_win FROM sessions WHERE id = $1", session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    points_to_win = session["points_to_win"]

    winners_row = await fetch("""
        SELECT username AS winners
        FROM users
        JOIN user_scores ON user_scores.user_id = users.id
        WHERE user_scores.session_id = $1 AND user_scores.score = $2
    """, session_id, points_to_win)

    if winners_row:
        winners = [row["winners"] for row in winners_row]
        leaderboard_rows = await fetch("""
            SELECT users.username, user_scores.score
            FROM users
            JOIN user_scores ON users.id = user_scores.user_id
            WHERE user_scores.session_id = $1
        """, session_id)
        leaderboard = [{"username": row["username"], "score": row["score"]} for row in leaderboard_rows]

        round_flags[(session_id, round)] = {
            "state": "game_over",
            "start_at": None,
            "end_at": None
        }

        await gather(
            execute("UPDATE sessions SET active = FALSE WHERE id = $1", session_id),
            *[
                execute("""
                    UPDATE user_scores
                    SET winner = TRUE
                    WHERE session_id = $1 AND user_id = (SELECT id FROM users WHERE username = $2)
                """, session_id, winner)
                for winner in winners
            ],
        )

        await broadcast("sessions", {
            "type": "session_deactivated",
            "session_id": session_id
        })

        await broadcast(f"session_{session_id}", {
            "type": "game_over",
            "winners": winners,
            "leaderboard": leaderboard
        })

        # add restrictions to host lobby/ waiting area and the game page to detect that the session is no longer active

        return JSONResponse({"status": "game_over", "winners": winners, "leaderboard": leaderboard})

    # ✅ Prepare next round
    next_round_number = round + 1
    await execute("""
        INSERT INTO rounds (session_id, round, started, ended)
        VALUES ($1, $2, FALSE, FALSE)
    """, session_id, next_round_number)

    # ✅ Init round state
    round_flags[(session_id, next_round_number)] = {
        "state": "new_round",
        "start_at": None,
        "end_at": None
    }

    await broadcast(f"session_{session_id}", {
        "type": "round_ended",
        "next_round": next_round_number
    })

    game_sentences_raw = await fetch("SELECT sentence FROM game_sentences WHERE session_id = $1", session_id)
    game_sentences = [row["sentence"] for row in game_sentences_raw]
    next_round_sentence = game_sentences[next_round_number - 1] if next_round_number - 1 < len(game_sentences) else "Statement unavailable"

    await broadcast(f"session_{session_id}", {
        "type": "new_round",
        "round": next_round_number,
        "next_round_sentence": next_round_sentence,
        "next_round_state": "new_round"
    })

    return JSONResponse({"status": "next_round_started", "round": next_round_number, "state": "new_round", "start_at": None})
