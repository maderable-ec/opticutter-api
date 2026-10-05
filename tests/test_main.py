import os
import subprocess
import sys

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from main import app
from src.shared.database import get_db

client = TestClient(app)


def _product_payload(code="MEL18"):
    return {
        "type": "board",
        "code": code,
        "name": f"Melamina {code}",
        "price": 45.5,
        "attributes": {"height": 2440, "width": 1220, "thickness": 18},
    }


def test_app_mappers_configure_without_extra_model_imports():
    """Regression: the app must configure SQLAlchemy mappers using only the
    models that ``main`` imports (via routers). A model that ``main`` does not
    transitively import but that another mapper references by string would fail
    only at runtime with a 500 ``failed to locate '<Model>'``.

    Runs in an isolated subprocess because ``conftest`` imports ALL models and
    would mask the problem.
    """
    code = (
        "import main; "
        "from sqlalchemy.orm import configure_mappers; "
        "configure_mappers()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env={**os.environ, "ENVIRONMENT": "local"},
    )
    assert result.returncode == 0, result.stderr


def test_root_redirect():
    """Test that the root route redirects to /docs"""
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"


def test_health_check():
    """Test of the basic health check endpoint"""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "environment" in data
    assert "version" in data


def test_api_health_check():
    """Test of the API health check endpoint"""
    response = client.get("/api/v1/health/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "environment" in data
    assert "version" in data


def test_readiness_check():
    """Test of the readiness check endpoint"""
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert "checks" in data


def test_the_old_cutter_placeholders_are_gone():
    """They answered fixed data (``active_processes: 0``) under the project's old
    name, and nothing read them: health lives at ``/health`` and ``/api/v1/health/``."""
    assert client.get("/api/v1/cutter/").status_code == 404
    assert client.get("/api/v1/cutter/status").status_code == 404


# The previous web build still calls these paths from any tab left open across the
# deploy. Each one answers through the very handler of its new path, out of the
# OpenAPI schema; this table goes, with the aliases, in the release after.
_DEPRECATED_ALIASES = [
    ("GET", "/api/v1/analytics/low-stock", "GET", "/api/v1/inventory/low-stock"),
    ("GET", "/api/v1/analytics/users", "GET", "/api/v1/analytics/productivity"),
    (
        "GET",
        "/api/v1/orders/{order_id}/export",
        "GET",
        "/api/v1/orders/{order_id}/billing-export",
    ),
    (
        "POST",
        "/api/v1/orders/{order_id}/invoice",
        "PATCH",
        "/api/v1/orders/{order_id}/invoice",
    ),
]


@pytest.mark.parametrize("old_method,old_path,new_method,new_path", _DEPRECATED_ALIASES)
def test_a_deprecated_alias_serves_its_new_route_out_of_the_schema(
    old_method, old_path, new_method, new_path
):
    def route(method, path):
        return next(
            r
            for r in app.routes
            if isinstance(r, APIRoute) and r.path == path and method in r.methods
        )

    old, new = route(old_method, old_path), route(new_method, new_path)
    assert old.endpoint is new.endpoint
    assert old.include_in_schema is False
    assert new.include_in_schema is True


def test_success_response_has_meta_and_request_id_header(client):
    """Every success response carries ``meta`` and echoes the requestId in the header."""
    resp = client.post("/api/v1/products/", json=_product_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert "data" in body
    meta = body["meta"]
    assert meta["requestId"]
    assert meta["timestamp"]
    assert resp.headers["x-request-id"] == meta["requestId"]


def test_incoming_request_id_is_propagated(client):
    """An incoming ``X-Request-ID`` is preserved (trace continuity) and lands in meta."""
    resp = client.get("/api/v1/products/999999", headers={"X-Request-ID": "trace-123"})
    assert resp.status_code == 404
    assert resp.headers["x-request-id"] == "trace-123"
    assert resp.json()["meta"]["requestId"] == "trace-123"


def test_validation_error_includes_field_path(client):
    """Request validation → 422 with ``code`` and ``field`` shaped like ``body.<field>``."""
    # Valid ``type`` (discriminator) with the rest of the fields missing: the error
    # points to the product's subfields (``body.board.<field>``).
    resp = client.post("/api/v1/products/", json={"type": "board"})
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert errors[0]["code"] == "VALIDATION_ERROR"
    assert any(e["field"] and e["field"].startswith("body.") for e in errors)


def test_unknown_route_returns_404_envelope(client):
    """A nonexistent route also responds with the ``{errors, meta}`` envelope."""
    resp = client.get("/api/v1/nope")
    assert resp.status_code == 404
    body = resp.json()
    assert body["errors"][0]["code"] == "NOT_FOUND"
    assert body["meta"]["requestId"]


def test_unhandled_exception_returns_500_envelope(db_session, monkeypatch):
    """An unhandled exception is translated into a wrapped 500, without leaking details."""

    def _boom(self, id):
        raise RuntimeError("boom")

    from src.modules.products.service import ProductService
    from src.modules.users.schemas import UserCreate
    from src.modules.users.service import UserService

    monkeypatch.setattr(ProductService, "get_or_404", _boom)

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            # The endpoint requires auth: authenticate as admin to reach the handler
            # (where the monkeypatch triggers the unhandled exception).
            UserService(db_session).create(
                UserCreate(
                    email="boom-admin@empresa.com",
                    password="boom-admin-pwd",
                    roles=["administrador"],
                    full_name="Boom",
                )
            )
            token = test_client.post(
                "/api/v1/auth/login",
                json={"email": "boom-admin@empresa.com", "password": "boom-admin-pwd"},
            ).json()["data"]["accessToken"]
            resp = test_client.get(
                "/api/v1/products/1", headers={"Authorization": f"Bearer {token}"}
            )
        assert resp.status_code == 500
        body = resp.json()
        assert body["errors"][0]["code"] == "INTERNAL_SERVER_ERROR"
        assert body["errors"][0]["message"] == "Error interno del servidor"
        assert body["meta"]["requestId"]
    finally:
        app.dependency_overrides.clear()
