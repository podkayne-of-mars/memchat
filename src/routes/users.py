"""User management endpoints — login, logout, user switching."""

import hashlib

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from src.auth import clear_session, get_current_user_id, set_user_session
from src.database import create_user, get_user, get_user_by_username, list_users, set_persona
from src.models import UserCreate

DEFAULT_PERSONA = (
    "You are a helpful assistant in an ongoing conversation. "
    "Be practical, direct, and focused on being useful. "
    "Remember previous discussions, decisions made, and ideas explored "
    "— including ones that were rejected and why."
)

router = APIRouter(tags=["users"])
templates = Jinja2Templates(directory="templates")


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


@router.get("/login")
async def login_page(
    request: Request,
    error: str | None = None,
    user_id: int | None = None,
):
    """Serve the login/user selection page. Redirect to /chat if already logged in.

    If user_id is set, show the password prompt for that user instead of the
    user list.
    """
    current = get_current_user_id(request)
    if current is not None:
        return RedirectResponse(url="/chat", status_code=303)

    # Password prompt for a specific user
    selected_user = None
    if user_id is not None:
        selected_user = get_user(user_id)

    return templates.TemplateResponse("login.html", {
        "request": request,
        "users": list_users(),
        "error": error,
        "selected_user": selected_user,
    })


@router.post("/login")
async def login(
    request: Request,
    user_id: int = Form(...),
    password: str = Form(""),
):
    """Verify password, set session cookie, redirect to chat."""
    user = get_user(user_id)
    if not user:
        return RedirectResponse(url="/login?error=User+not+found", status_code=303)

    stored = user.get("password_hash", "")
    if stored and _hash_password(password) != stored:
        return RedirectResponse(
            url=f"/login?user_id={user_id}&error=Wrong+password", status_code=303
        )

    set_user_session(request, user_id)
    return RedirectResponse(url="/chat", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    clear_session(request)
    return RedirectResponse(url="/login", status_code=303)


@router.post("/api/users/create-and-login")
async def create_and_login(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(""),
):
    """Create a new user with default persona, set cookie, redirect to chat."""
    existing = get_user_by_username(username)
    if existing:
        return RedirectResponse(
            url="/login?error=Username+already+exists", status_code=303
        )
    pw_hash = _hash_password(password) if password else ""
    user_id = create_user(username, display_name, password_hash=pw_hash)
    set_persona(user_id, DEFAULT_PERSONA)
    set_user_session(request, user_id)
    return RedirectResponse(url="/chat", status_code=303)


# --- JSON API (unchanged) ---

@router.get("/api/users")
async def get_users():
    """List all users."""
    return list_users()


@router.post("/api/users", status_code=201)
async def create_new_user(req: UserCreate):
    """Create a new user."""
    existing = get_user_by_username(req.username)
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")
    user_id = create_user(req.username, req.display_name)
    return get_user(user_id)


@router.get("/api/users/{user_id}")
async def get_user_detail(user_id: int):
    """Get a single user."""
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user
