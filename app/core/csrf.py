import secrets

from fastapi import HTTPException, Request, status

CSRF_SESSION_KEY = "csrf_token"


def get_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


async def require_csrf(request: Request) -> None:
    expected = request.session.get(CSRF_SESSION_KEY)
    form = await request.form()
    supplied = form.get("csrf_token")
    if not expected or not supplied or not secrets.compare_digest(str(expected), str(supplied)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token invalid")
