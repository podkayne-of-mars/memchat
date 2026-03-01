"""Settings endpoints — persona editing and configuration viewing."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.auth import get_current_user_id, require_login
from src.database import get_active_persona, get_user, set_persona
from src.models import PersonaUpdate

router = APIRouter(tags=["settings"])
templates = Jinja2Templates(directory="templates")


@router.get("/settings")
async def settings_page(request: Request):
    """Render the settings page with persona textarea."""
    result = require_login(request)
    if isinstance(result, RedirectResponse):
        return result
    user = get_user(result)
    persona = get_active_persona(result)
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "persona_text": persona["persona_text"] if persona else "",
    })


@router.get("/api/settings/persona")
async def get_persona(request: Request):
    """Get the active persona for the logged-in user."""
    user_id = get_current_user_id(request)
    if user_id is None:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    persona = get_active_persona(user_id)
    if not persona:
        return {"persona_text": ""}
    return persona


@router.put("/api/settings/persona")
async def update_persona(req: PersonaUpdate, request: Request):
    """Update the active persona for the logged-in user."""
    user_id = get_current_user_id(request)
    if user_id is None:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    new_id = set_persona(user_id, req.persona_text)
    return {"id": new_id, "status": "updated"}
