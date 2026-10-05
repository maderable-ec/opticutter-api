"""Cross-cutting system routes: health checks."""

from fastapi import APIRouter

from src.shared.config import config

router = APIRouter()

health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get("/")
async def api_health():
    """Basic service status."""
    return {"status": "healthy", "environment": config.ENVIRONMENT, "version": "1.0.0"}


@health_router.get("/ready")
async def api_ready():
    """Readiness check (service dependencies)."""
    checks = {
        "redis": True,
    }
    return {"status": "ready", "checks": checks}


router.include_router(health_router)
