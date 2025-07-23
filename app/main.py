# app/main.py
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from app.routes import websock
from app.routes import auth
from app.routes import dashboard
from app.auth_utils import get_current_user
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)

app = FastAPI()

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(websock.router)
app.include_router(auth.router)
app.include_router(dashboard.router)

@app.get("/welcome")
async def welcome(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("welcome.html", {"request": request, "user": user})
