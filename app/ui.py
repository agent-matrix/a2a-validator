# app/ui.py

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import httpx
import os
import json

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Tabs to render in the UI. "Info" is now the active tab for the /home route.
NAV_TABS = [
    {"href": "/validator", "label": "Validator"},
    {"href": "/home", "label": "Info"},
]
templates.env.globals["NAV_TABS"] = NAV_TABS

def _self_base_url() -> str:
    port = os.getenv("PORT", "7860")
    return f"http://127.0.0.1:{port}"

@router.get("/", include_in_schema=False)
async def root_redirect():
    # Default to the Validator page
    return RedirectResponse(url="/validator", status_code=302)

@router.get("/home", response_class=HTMLResponse, include_in_schema=False)
async def home_page(request: Request):
    """
    FIX: This route now correctly serves the home.html template
    instead of getting caught in a redirect loop.
    """
    return templates.TemplateResponse(
        "home.html",
        # Pass the active tab name to the template
        {"request": request, "tabs": NAV_TABS, "active": "home"},
    )

# The /chat and /dev routes are not needed based on your last request,
# so they have been removed to simplify the file.
# If you need them back, you can uncomment them.

# @router.get("/chat", response_class=HTMLResponse)
# async def chat_get(request: Request):
#     return templates.TemplateResponse(
#         "chat.html",
#         {"request": request, "answer": None, "tabs": NAV_TABS, "active": "chat"},
#     )

# @router.get("/dev", response_class=HTMLResponse)
# async def dev_get(request: Request):
#     # ... dev page logic ...
#     pass