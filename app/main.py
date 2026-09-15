import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import audit, auth, health, users
from app.core.config import get_settings
from app.core.request_limits import RequestBodyLimitMiddleware

logging.basicConfig(level=logging.INFO)

settings = get_settings()


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                existing = {name.lower() for name, _ in headers}

                def add(name: str, value: str) -> None:
                    name_bytes = name.lower().encode("latin-1")
                    if name_bytes not in existing:
                        headers.append((name_bytes, value.encode("latin-1")))
                        existing.add(name_bytes)

                add("X-Content-Type-Options", "nosniff")
                add("X-Frame-Options", "DENY")
                add("Referrer-Policy", "same-origin")
                add(
                    "Content-Security-Policy",
                    "default-src 'self'; "
                    "script-src 'self' 'unsafe-inline'; "
                    "style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; "
                    "base-uri 'none'; "
                    "frame-ancestors 'none'; "
                    "form-action 'self'",
                )
                scheme = scope.get("scheme")
                if scheme == "https":
                    add("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            await send(message)

        await self.app(scope, receive, send_with_headers)


app = FastAPI(title="Quick Account Manager")

app.add_middleware(RequestBodyLimitMiddleware, max_bytes=settings.max_upload_bytes + 64 * 1024)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key,
    same_site="lax",
    https_only=settings.session_cookie_secure and settings.environment != "development",
)
app.add_middleware(SecurityHeadersMiddleware)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(audit.router)
