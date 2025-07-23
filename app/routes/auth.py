from fastapi import APIRouter, Form, Request, HTTPException
from fastapi.responses import RedirectResponse
from app.db import connect_db, fetchrow, fetch, execute
from app.auth_utils import hash_password, verify_password, create_session_cookie, get_current_user, is_password_complex
from fastapi.templating import Jinja2Templates
import traceback

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()

@router.get("/login")
async def login_get(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "user": user})

@router.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    try:
        user_row = await fetchrow("SELECT * FROM users WHERE username = $1", username)

        if not user_row or not verify_password(password, user_row["hash"]):
            raise ValueError("Invalid credentials")

        response = RedirectResponse("/", status_code=302)
        response.set_cookie("session", create_session_cookie(username))
        return response

    except Exception as e:
        print("\n[LOGIN ERROR]")
        traceback.print_exc()

        return templates.TemplateResponse(
            "login.html",
            {"request": request, "user": None, "error": "Invalid credentials"}
        )

@router.get("/register")
async def register_get(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("register.html", {"request": request, "user": user})

@router.post("/register")
async def register_post(request: Request, username: str = Form(...), password: str = Form(...), confirmation: str = Form(...)):
    user = get_current_user(request)
    
    # Password match validation
    if password != confirmation:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "user": None, "error": "Passwords do not match"}
        )
    
    if not is_password_complex(password):
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "user": None,
                "error": "Password must be at least 8 characters long and include an uppercase letter, a digit, and a symbol (@$!%*?&)."
            }
        )

    existing = await fetchrow("SELECT * FROM users WHERE username = $1", username)

    if existing:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "user": None, "error": "User already exists"}
        )

    hashed = hash_password(password)
    await execute("INSERT INTO users (username, hash) VALUES ($1, $2)", username, hashed)

    response = RedirectResponse("/", status_code=302)
    response.set_cookie("session", create_session_cookie(username))
    return response

@router.get("/logout")
async def logout():
    response = RedirectResponse("/welcome", status_code=302)
    response.delete_cookie("session")
    return response
