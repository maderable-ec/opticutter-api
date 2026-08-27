import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from src.modules.additional_services.router import (
    router as additional_services_router,
)
from src.modules.analytics.router import router as analytics_router
from src.modules.branches.router import router as branches_router
from src.modules.clients.router import router as clients_router
from src.modules.notifications.router import router as notifications_router
from src.modules.optimization_drafts.router import router as optimization_drafts_router
from src.modules.optimizations.engine_info import (
    backend_name,
    degraded_warning,
    engine_summary,
)
from src.modules.optimizations.parallel import (
    shutdown_pool_executor,
    warmup_pool_executor,
)
from src.modules.optimizations.router import router as optimizations_router
from src.modules.orders.router import router as orders_router
from src.modules.preorders.public_router import router as preorders_public_router
from src.modules.preorders.router import router as preorders_router
from src.modules.print_jobs.router import router as print_router
from src.modules.products.router import router as products_router
from src.modules.settings.router import router as settings_router
from src.modules.settings.router import tiers_router as settings_tiers_router
from src.modules.system.router import router as system_router
from src.modules.users.auth_router import router as auth_router
from src.modules.users.router import router as users_router
from src.shared.config import config
from src.shared.errors import register_exception_handlers
from src.shared.middleware import CurrentUserMiddleware, RequestIDMiddleware

# Configurar logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle handling"""
    logger.info("Starting FastAPI application")
    # WARNING, not INFO: production runs at LOG_LEVEL=WARNING, and this line is
    # the only evidence of which engine the box actually runs. Both the native
    # packing kernel and CP-SAT degrade silently when their optional dependency
    # is missing, and the packing one costs ~3x.
    logger.warning("%s", engine_summary())
    degraded = degraded_warning()
    if degraded:
        logger.warning("%s", degraded)
    # Pay for the forkserver and its ortools preload now, not on a customer's
    # first quote. Best-effort by construction: the pool falls back to in-process
    # optimization, so booting must never depend on it. The warm-up doubles as
    # the worker's own engine report — the packing happens in those children.
    warmup_pool_executor(parent_packing=backend_name())
    yield
    shutdown_pool_executor()
    logger.info("Shutting down FastAPI application")


# Interactive docs (Swagger/ReDoc) and the OpenAPI schema are disabled in
# production to reduce the exposed surface; enabled in every other environment.
_DOCS_ENABLED = config.ENVIRONMENT != "production"

# Create FastAPI application
app = FastAPI(
    title="Cutter API",
    description="API for optimizing melamine board cuts",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)

# Middlewares
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Authenticated user in context for generic auditing (created_by/updated_by).
app.add_middleware(CurrentUserMiddleware)
# Per-request correlation (requestId + X-Request-ID header). Added last so it
# wraps the rest of the stack and is available on both success and error.
app.add_middleware(RequestIDMiddleware)

# Centralized application error handling
register_exception_handlers(app)

# Include routes
app.include_router(system_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(users_router, prefix="/api/v1")
app.include_router(branches_router, prefix="/api/v1")
app.include_router(products_router, prefix="/api/v1")
app.include_router(clients_router, prefix="/api/v1")
app.include_router(additional_services_router, prefix="/api/v1")
app.include_router(optimizations_router, prefix="/api/v1")
app.include_router(optimization_drafts_router, prefix="/api/v1")
app.include_router(orders_router, prefix="/api/v1")
app.include_router(print_router, prefix="/api/v1")
app.include_router(preorders_router, prefix="/api/v1")
app.include_router(preorders_public_router, prefix="/api/v1")
app.include_router(analytics_router, prefix="/api/v1")
app.include_router(notifications_router, prefix="/api/v1")
app.include_router(settings_router, prefix="/api/v1")
app.include_router(settings_tiers_router, prefix="/api/v1")


@app.get("/")
async def root():
    """Redirects to the API docs (when enabled), else reports service status."""
    if _DOCS_ENABLED:
        return RedirectResponse("/docs")
    return {"status": "ok", "service": "Cutter API"}


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "environment": config.ENVIRONMENT, "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        reload=True if config.ENVIRONMENT == "local" else False,
        log_level=config.LOG_LEVEL.lower(),
    )
