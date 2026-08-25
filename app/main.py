import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api import audit, auth, health, users
from app.core.config import get_settings

logging.basicConfig(level=logging.INFO)

settings = get_settings()

app = FastAPI(title="Quick Account Manager")

app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, same_site="lax")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(audit.router)
