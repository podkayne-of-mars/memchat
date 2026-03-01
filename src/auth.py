"""Cookie-based session auth for multi-user support."""

from fastapi import Request
from fastapi.responses import RedirectResponse


def get_current_user_id(request: Request) -> int | None:
    """Read user_id from the signed session cookie. Returns None if not set."""
    return request.session.get("user_id")


def require_login(request: Request) -> int | RedirectResponse:
    """Return user_id if logged in, or a redirect to /login."""
    user_id = get_current_user_id(request)
    if user_id is None:
        return RedirectResponse(url="/login", status_code=303)
    return user_id


def set_user_session(request: Request, user_id: int) -> None:
    """Set user_id in the session cookie."""
    request.session["user_id"] = user_id


def clear_session(request: Request) -> None:
    """Clear the session cookie."""
    request.session.clear()
