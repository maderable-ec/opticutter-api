"""Shared fixtures: app with a PostgreSQL database isolated per test."""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# Importing the models populates ``Base.metadata`` before ``create_all``.
import src.modules.additional_services.model  # noqa: F401,E402
import src.modules.branches.model  # noqa: F401,E402
import src.modules.clients.model  # noqa: F401,E402
import src.modules.notifications.model  # noqa: F401,E402
import src.modules.optimization_drafts.model  # noqa: F401,E402
import src.modules.orders.model  # noqa: F401,E402
import src.modules.preorders.model  # noqa: F401,E402
import src.modules.print_jobs.model  # noqa: F401,E402
import src.modules.products.model  # noqa: F401,E402
import src.modules.settings.model  # noqa: F401,E402
import src.modules.users.login_event_model  # noqa: F401,E402
import src.modules.users.model  # noqa: F401,E402
import src.modules.users.refresh_token_model  # noqa: F401,E402
from main import app
from src.modules.branches.model import BranchModel
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService
from src.shared.cache import cache
from src.shared.database import Base, get_db
from src.shared.security import create_access_token

# Admin credentials used by the default authenticated client (see ``client``).
_CONFTEST_ADMIN_EMAIL = "conftest-admin@empresa.com"
_CONFTEST_ADMIN_PWD = "conftest-admin-pwd"

# Default branch seeded into every test database (first insert => stable id).
# Suites reference it when creating orders/pre-orders/drafts/staff users.
DEFAULT_BRANCH_ID = 1

_TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql://cutter:cutter@localhost:5433/cutter_db"),
)

_test_engine = create_engine(_TEST_DATABASE_URL)


@pytest.fixture(scope="session")
def _create_schema():
    """Creates the schema once per pytest session (idempotent).

    Not ``autouse``: only requested by fixtures that touch the database
    (``db_session`` and, transitively, ``client``/``anon_client``). This way the
    unit tests under ``tests/unit/`` (marked ``unit``) run without opening a
    PostgreSQL connection.
    """
    Base.metadata.create_all(_test_engine)


@pytest.fixture
def db_session(_create_schema):
    """PostgreSQL session isolated per test: TRUNCATE at start + branch seed."""
    table_names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with _test_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))

    session = Session(_test_engine)
    session.add(BranchModel(code="MATRIZ", name="Casa Matriz", is_active=True))
    session.commit()
    try:
        yield session
    finally:
        session.close()


class _InMemoryRedis:
    """In-memory Redis double: mirrors ``get``/``set`` over JSON strings."""

    def __init__(self):
        self._store: dict = {}

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, ex=None):
        self._store[key] = value
        return True


@pytest.fixture(autouse=True)
def isolated_cache():
    """Isolates the real Redis cache: each test uses a clean in-memory double."""
    original_client, original_initialized = cache._client, cache._initialized
    cache._client = _InMemoryRedis()
    cache._initialized = True
    try:
        yield
    finally:
        cache._client, cache._initialized = original_client, original_initialized


@pytest.fixture
def anon_client(db_session):
    """Unauthenticated TestClient, with ``get_db`` over the test's isolated database.

    For the auth tests (login, refresh, role enforcement), which control the
    ``Authorization`` header per request. The rest of the suites use ``client``.
    """

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def client(anon_client, db_session):
    """TestClient authenticated as an administrator by default.

    RBAC enforcement protects almost every endpoint; so each module's suite can
    test its logic without auth friction, this client attaches a seeded admin's
    Bearer token by default. Tests that need to control authentication
    (``test_users.py``) use ``anon_client``.
    """
    svc = UserService(db_session)
    admin = svc.get_by_email(_CONFTEST_ADMIN_EMAIL)
    if admin is None:
        admin = svc.create(
            UserCreate(
                email=_CONFTEST_ADMIN_EMAIL,
                password=_CONFTEST_ADMIN_PWD,
                roles=["administrador"],
                full_name="Conftest Admin",
            )
        )
    # Mint the JWT directly instead of hitting /auth/login: ``get_current_user``
    # resolves the live role via ``sub`` (user id), so neither the bcrypt verify
    # nor the login's HTTP round-trip is needed on every test.
    token = create_access_token(admin.id, admin.roles)
    anon_client.headers.update({"Authorization": f"Bearer {token}"})
    return anon_client


def pytest_collection_modifyitems(config, items):
    """Auto-marks by path: ``tests/unit/`` => ``unit``; everything else => ``integration``.

    Avoids hand-annotating existing files and enables the fast loop
    ``pytest -m unit`` (no PostgreSQL) versus ``pytest -m integration`` (the
    current integration suite). Markers are declared in ``pyproject.toml``
    (``--strict-markers`` is on).
    """
    for item in items:
        parts = item.path.parts
        idx = parts.index("tests") if "tests" in parts else -1
        is_unit = idx != -1 and len(parts) > idx + 1 and parts[idx + 1] == "unit"
        item.add_marker("unit" if is_unit else "integration")
