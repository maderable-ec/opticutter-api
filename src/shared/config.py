import warnings

from environs import Env

env = Env()

# Insecure placeholder used only when SECRET_KEY is unset outside production.
_DEV_SECRET_PLACEHOLDER = "dev-secret-change-me"


class Config:
    """Single application configuration.

    Reads from environment variables; defaults depend on ``ENVIRONMENT``.
    Replaces the old split into ``core/config/{base,local,staging,production}.py``.
    """

    ENVIRONMENT = env("ENVIRONMENT", "local")

    LOG_LEVEL = env(
        "LOG_LEVEL",
        {"local": "DEBUG", "staging": "INFO", "production": "WARNING"}.get(
            ENVIRONMENT, "INFO"
        ),
    )
    HOST = env("HOST", "0.0.0.0")
    PORT = env.int("PORT", 8000)

    DEFAULT_TIMEZONE = env("DEFAULT_TIMEZONE", "America/Guayaquil")

    CORS_ORIGINS = env.list(
        "CORS_ORIGINS",
        [
            "http://localhost:3000",
            "http://localhost:3001",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:3001",
        ],
    )

    DATABASE_URL = env("DATABASE_URL")

    # Connection pool tuning (SQLAlchemy QueuePool). Defaults match SQLAlchemy's
    # own (5 + 10) but are exposed so a busier deployment can raise them.
    # DB_POOL_RECYCLE_SECONDS bounds a connection's lifetime (30 min) to avoid
    # stale sockets behind managed Postgres/pgbouncer idle timeouts.
    DB_POOL_SIZE = env.int("DB_POOL_SIZE", 5)
    DB_MAX_OVERFLOW = env.int("DB_MAX_OVERFLOW", 10)
    DB_POOL_RECYCLE_SECONDS = env.int("DB_POOL_RECYCLE_SECONDS", 1800)

    REDIS_URL = env(
        "REDIS_URL",
        {
            "local": "redis://localhost:6379/0",
            "staging": "redis://redis-staging:6379/0",
            "production": "redis://redis-prod:6379/0",
        }.get(ENVIRONMENT, "redis://localhost:6379/0"),
    )

    SECRET_KEY = (
        env("SECRET_KEY")
        if ENVIRONMENT == "production"
        else env("SECRET_KEY", _DEV_SECRET_PLACEHOLDER)
    )
    # Any non-local, non-production deployment (e.g. staging) that still relies on
    # the placeholder ships forgeable JWTs: warn loudly so it can't slip to an
    # internet-exposed host unnoticed (production already hard-fails: no default).
    if (
        ENVIRONMENT not in ("production", "local")
        and SECRET_KEY == _DEV_SECRET_PLACEHOLDER
    ):
        warnings.warn(
            "SECRET_KEY is the insecure development placeholder; set a strong "
            "SECRET_KEY before exposing this environment.",
            stacklevel=2,
        )

    # JWT (authentication). HS256 signs with SECRET_KEY; the access token expires
    # after ACCESS_TOKEN_EXPIRE_MINUTES. In production SECRET_KEY is mandatory
    # (above); other environments fall back to a development placeholder. The
    # access token is short-lived (renewable via /auth/refresh): the frontend
    # presents the refresh token and gets a new pair. REFRESH_TOKEN_EXPIRE_DAYS
    # bounds the refresh token's lifetime (opaque, revocable). Kept at 1 day as a
    # defense-in-depth backstop: the frontend already forces a real /auth/login
    # once per calendar day (attendance is only recorded on that endpoint, never
    # on refresh), so in normal operation a refresh token never gets older than a
    # day anyway — this just bounds abandoned sessions that skip that guard
    # (stale cached bundle, tampered localStorage). JWT_ISSUER/AUDIENCE are
    # stamped and validated on every token (defense in depth against tokens
    # minted for/by another service reusing the same secret).
    JWT_ALGORITHM = env("JWT_ALGORITHM", "HS256")
    JWT_ISSUER = env("JWT_ISSUER", "cutter-api")
    JWT_AUDIENCE = env("JWT_AUDIENCE", "cutter-app")
    ACCESS_TOKEN_EXPIRE_MINUTES = env.int("ACCESS_TOKEN_EXPIRE_MINUTES", 30)
    REFRESH_TOKEN_EXPIRE_DAYS = env.int("REFRESH_TOKEN_EXPIRE_DAYS", 1)

    # bcrypt cost factor when hashing passwords (rounds = 2**factor iterations).
    # 12 is a safe default for prod/dev; the test suite lowers it to 4 (still valid
    # hashes, ~16x faster) to avoid paying bcrypt's deliberate cost in every fixture.
    BCRYPT_ROUNDS = env.int("BCRYPT_ROUNDS", 12)

    # First admin seed, applied by the idempotent script ``scripts/seed_admin.py``
    # (run via ``make seed-admin``), NOT by a migration. If both are set and no
    # user exists with that email, the script creates it with role
    # "administrador". Empty = nothing is seeded.
    ADMIN_EMAIL = env("ADMIN_EMAIL", "")
    ADMIN_PASSWORD = env("ADMIN_PASSWORD", "")

    # Cutting parameters (mm). Only seed the `settings` singleton row on its first
    # read; the runtime source of truth is the `settings` table (editable via
    # PATCH /settings/cutting).
    KERF = env.float("KERF", 5.0)
    TOP_TRIM = env.float("TOP_TRIM", 0.0)
    BOTTOM_TRIM = env.float("BOTTOM_TRIM", 0.0)
    LEFT_TRIM = env.float("LEFT_TRIM", 0.0)
    RIGHT_TRIM = env.float("RIGHT_TRIM", 0.0)

    # Edge banding: waste (offcut from the edge-banding machine) applied to the net
    # length before rounding up to the whole meter that gets billed. 0.10 = +10%.
    EDGE_BANDING_WASTE_FACTOR = env.float("EDGE_BANDING_WASTE_FACTOR", 0.10)

    # Half boards (medio tablero): markup applied over price/2 when billing a job
    # as half a catalog board. 0.10 = +10%. Only seeds the `settings` singleton
    # row on its first read; the runtime source of truth is the `settings` table
    # (editable via PATCH /settings/cutting).
    HALF_BOARD_MARKUP_PCT = env.float("HALF_BOARD_MARKUP_PCT", 0.10)

    OPT_RESULT_TTL_SECONDS = env.int("OPT_RESULT_TTL_SECONDS", 259200)

    # Board-count search budget (src/cutting/search.py). Counted in candidate
    # fills per board / improvement iterations — never wall clock — so results
    # stay deterministic and cacheable. Both feed the optimization hash: change
    # them and cached results are recomputed.
    OPT_TRIES_PER_BOARD = env.int("OPT_TRIES_PER_BOARD", 48)
    OPT_SEARCH_ITERATIONS = env.int("OPT_SEARCH_ITERATIONS", 40)

    # CP-SAT exact endgame (src/cutting/exact.py). The heuristics measurably
    # fail to close packs above ~94%, which is exactly where a board is won or
    # lost; a solver decides those few states exactly. Budgeted in solver CALLS
    # and OR-Tools "deterministic time" (a work unit, not seconds) so the result
    # never depends on machine load — the payload is cached by input hash. These
    # also feed that hash: change them and cached results are recomputed.
    # OPT_EXACT_ENABLED=false falls back to the pure-heuristic search.
    OPT_EXACT_ENABLED = env.bool("OPT_EXACT_ENABLED", True)
    OPT_EXACT_MAX_PIECES = env.int("OPT_EXACT_MAX_PIECES", 120)
    OPT_EXACT_MAX_CALLS = env.int("OPT_EXACT_MAX_CALLS", 40)
    OPT_EXACT_DETERMINISTIC_TIME = env.float("OPT_EXACT_DETERMINISTIC_TIME", 6.0)

    # Order attachments (anexos): PDFs/screenshots stored on local disk under
    # ATTACHMENTS_DIR (one subfolder per order). Only their metadata lives in
    # Postgres; the bytes stay on the filesystem (a Docker volume in prod).
    # Allowed types are closed to PDF + PNG/JPEG; MAX_ATTACHMENT_MB caps each file.
    ATTACHMENTS_DIR = env("ATTACHMENTS_DIR", "uploads")
    MAX_ATTACHMENT_MB = env.int("MAX_ATTACHMENT_MB", 10)
    ATTACHMENT_ALLOWED_TYPES = env.list(
        "ATTACHMENT_ALLOWED_TYPES",
        ["application/pdf", "image/png", "image/jpeg"],
    )

    # Pre-orders (mutable quote): validity period and open-count cap per client.
    # Only seed the "preorders" section of the `settings` singleton row on its
    # first read; the runtime source of truth is the `settings` table (editable
    # via PATCH /settings/preorders), same as the cutting parameters.
    PREORDER_VALIDITY_DAYS = env.int("PREORDER_VALIDITY_DAYS", 15)
    MAX_OPEN_PREORDERS_PER_CLIENT = env.int("MAX_OPEN_PREORDERS_PER_CLIENT", 5)

    # Price tiers (discount over the base "Precio Consumidor" price). Only seed
    # the `price_tiers` column of the `settings` singleton row on its first read;
    # the runtime source of truth is the `settings` table (editable via PATCH
    # /settings/price-tiers). The discount applies only to catalog boards and is
    # frozen into the order (historical audit even if rates change later).
    PRICE_TIERS = env.json(
        "PRICE_TIERS",
        [
            {
                "code": "consumidor",
                "name": "Precio Consumidor",
                "rate": 0.0,
                "is_active": True,
                "sort_order": 1,
            },
            {
                "code": "carpintero",
                "name": "Precio Carpintero",
                "rate": 0.02,
                "is_active": True,
                "sort_order": 2,
            },
            {
                "code": "efectivo",
                "name": "Precio Efectivo",
                "rate": 0.05,
                "is_active": True,
                "sort_order": 3,
            },
        ],
    )

    # Maderable frontend base: composes the review link URL the client opens (the
    # origin must also be in CORS_ORIGINS). The dashboard uses HashRouter, hence
    # the base ends in "/#" (route = {base}/review/{token}).
    FRONTEND_BASE_URL = env("FRONTEND_BASE_URL", "http://localhost:3000")

    # Company data (proforma letterhead). Dummy defaults that only seed the
    # `settings` singleton row on its first read; the runtime source of truth is
    # the `settings` table (editable via PATCH /settings/company).
    COMPANY_NAME = env("COMPANY_NAME", "Mi Empresa")
    COMPANY_TAGLINE = env("COMPANY_TAGLINE", "eslogan de la empresa")
    COMPANY_EMAIL = env("COMPANY_EMAIL", "correo@empresa.com")
    COMPANY_PHONE = env("COMPANY_PHONE", "0990000000 / 0990000001")
    COMPANY_BRANCHES = env.json(
        "COMPANY_BRANCHES",
        [
            {"name": "Sucursal 1", "address": "Calle Principal y Secundaria"},
            {"name": "Sucursal 2", "address": "Av. Central y Transversal"},
        ],
    )

    # Print agent (silent label/consolidated printing). Payloads (TSPL labels,
    # consolidated PDFs) are rendered server-side and spooled to local disk under
    # PRINT_SPOOL_DIR, one subfolder per branch; only the PrintJob metadata lives
    # in Postgres (same split as order attachments; a Docker volume in prod).
    PRINT_SPOOL_DIR = env("PRINT_SPOOL_DIR", "print_spool")
    # A job no agent claims within PRINT_JOB_TTL_MINUTES expires (the shop PC was
    # off): it won't print stale later.
    PRINT_JOB_TTL_MINUTES = env.int("PRINT_JOB_TTL_MINUTES", 10)
    # A claimed ('sent') job with no ack within this window is re-queued (the
    # agent died mid-print): at-least-once delivery.
    PRINT_JOB_VISIBILITY_TIMEOUT_SECONDS = env.int(
        "PRINT_JOB_VISIBILITY_TIMEOUT_SECONDS", 60
    )
    # How long GET /print/jobs/next holds the long-poll before returning 204. Kept
    # under the agent's HTTP timeout (30s) so the connection never times out.
    PRINT_POLL_WAIT_SECONDS = env.int("PRINT_POLL_WAIT_SECONDS", 25)
    # Spool disk hygiene. The per-job removal (ack, or the TTL sweep) can't reach
    # files whose row was deleted by an ondelete=CASCADE (Postgres drops the row
    # without running Python) or that predate the cleanup; a periodic mtime sweep
    # backstops them. A payload is only ever needed while its job is unexpired
    # (created + TTL), so anything older is safe to reclaim -- keep this
    # >= PRINT_JOB_TTL_MINUTES.
    PRINT_SPOOL_RETENTION_MINUTES = env.int("PRINT_SPOOL_RETENTION_MINUTES", 60)
    # The backstop runs on the agent long-poll; this throttles the directory walk to
    # at most once per interval per worker (0 = run on every sweep, used in tests).
    PRINT_SPOOL_SWEEP_INTERVAL_MINUTES = env.int(
        "PRINT_SPOOL_SWEEP_INTERVAL_MINUTES", 5
    )

    # Thermal label geometry (3nStar LTT334, direct thermal). The label is
    # rendered as a monochrome raster and emitted as a TSPL BITMAP, so these only
    # need to match the physical roll (confirmed against the printer at bring-up).
    PRINT_LABEL_WIDTH_MM = env.float("PRINT_LABEL_WIDTH_MM", 100.0)
    PRINT_LABEL_HEIGHT_MM = env.float("PRINT_LABEL_HEIGHT_MM", 50.0)
    PRINT_LABEL_GAP_MM = env.float("PRINT_LABEL_GAP_MM", 2.0)
    PRINT_LABEL_DPI = env.int("PRINT_LABEL_DPI", 203)
    # TSPL BITMAP convention is bit 0 = black dot, and PIL packs black pixels as 0,
    # so the raster maps directly. Flip this if a given unit prints inverted.
    PRINT_LABEL_INVERT = env.bool("PRINT_LABEL_INVERT", False)


config = Config()
