"""ESIP API: authentication, ticket investigation, chat, and administration."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.services.genai_transport import close_transport
from app.routers import (
    auth,
    tickets,
    ai,
    profile,
    feedback,
    analytics,
    admin,
    admin_architecture,
    announcements,
    docs_gap,
    telemetry,
    zaf_auth,
    universal,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info(
        "ESIP backend starting — project=%s model=%s cors=%s",
        settings.google_cloud_project_id,
        settings.gemini_model,
        settings.cors_origins_list,
    )
    try:
        yield
    finally:
        close_transport()


app = FastAPI(
    title="ESIP API",
    description="Enterprise Support Intelligence Platform — Backend",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=r"https://.*\.zdusercontent\.com",  # Zendesk ZAF iframe origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,     prefix="/api/auth",     tags=["auth"])
app.include_router(tickets.router,  prefix="/api/tickets",  tags=["tickets"])
app.include_router(ai.router,       prefix="/api/ai",       tags=["ai"])
app.include_router(profile.router,  prefix="/api/profile",  tags=["profile"])
app.include_router(feedback.router,   prefix="/api/feedback",   tags=["feedback"])
app.include_router(analytics.router,  prefix="/api/analytics",  tags=["analytics"])
app.include_router(admin.router,      prefix="/api/admin",      tags=["admin"])
app.include_router(admin_architecture.router, prefix="/api/admin/architecture", tags=["admin-architecture"])
app.include_router(announcements.router, prefix="/api/announcements", tags=["announcements"])
app.include_router(universal.router,  prefix="/api/universal",  tags=["universal"])
app.include_router(docs_gap.router,   prefix="/api/docs-gap",   tags=["docs-gap"])
app.include_router(telemetry.router,  prefix="/api/telemetry",  tags=["telemetry"])
app.include_router(zaf_auth.router,   prefix="/api/zaf",        tags=["zaf"])


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}
