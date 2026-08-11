"""Tests for the optimizations module: optimize flow (geometry + costs)."""

import pytest

from src.modules.optimizations import parallel
from src.modules.optimizations.schemas import OptimizeRequest
from src.modules.optimizations.service import OptimizationService
from src.shared.cache import cache
from src.shared.config import config
from src.shared.exceptions import ValidationError


def _create_client(client):
    return client.post(
        "/api/v1/clients/",
        json={
            "identifier": "0991112233",
            "firstName": "Ada",
            "lastName": "Lovelace",
            "phone": "0991112233",
        },
    ).json()["data"]


def _create_board(client, code="MEL18"):
    return client.post(
        "/api/v1/products/",
        json={
            "type": "board",
            "code": code,
            "name": f"Melamina {code}",
            "price": 45.5,
            "attributes": {"height": 2440, "width": 1220, "thickness": 18},
        },
    ).json()["data"]


def _optimize_payload(client_id, product_id):
    return {
        "clientId": client_id,
        "materials": [{"key": "b1", "source": "catalog", "productId": product_id}],
        "requirements": [
            {
                "priority": 0,
                "height": 400,
                "width": 600,
                "quantity": 2,
                "materialKey": "b1",
                "label": "Puerta",
                "canRotate": True,
            }
        ],
    }


def _full_board_payload(client_id, product_id, quantity=2):
    """Job requiring full board(s): piece with both sides > half width (610)."""
    return {
        "clientId": client_id,
        "materials": [{"key": "b1", "source": "catalog", "productId": product_id}],
        "requirements": [
            {
                "priority": 0,
                "height": 800,
                "width": 700,
                "quantity": quantity,
                "materialKey": "b1",
                "label": "Costado",
                "canRotate": True,
            }
        ],
    }


def test_optimize_returns_layouts(client):
    created_client = _create_client(client)
    created_board = _create_board(client)

    resp = client.post(
        "/api/v1/optimize/",
        json=_optimize_payload(created_client["id"], created_board["id"]),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["client"]["id"] == created_client["id"]
    assert data["totalBoardsUsed"] >= 1
    assert len(data["layouts"]) >= 1
    layout = data["layouts"][0]
    assert "placedPieces" in layout
    assert layout["material"]["materialKey"] == "b1"
    assert layout["material"]["sheetNumber"] == 1
    assert "efficiency" in layout["statistics"]
    assert layout["placedPieces"][0]["originalWidth"] == 600
    # Guillotine cuts exposed for drawing the saw lines.
    assert layout["cuts"], "placing pieces generates saw paths"
    assert set(layout["cuts"][0]) == {"x", "y", "length", "isHorizontal"}


def test_optimize_reports_cut_linear_meters(client):
    """The response exposes cut meters per sheet and the overall total (= sum)."""
    created_client = _create_client(client)
    created_board = _create_board(client)

    resp = client.post(
        "/api/v1/optimize/",
        json=_optimize_payload(created_client["id"], created_board["id"]),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert data["totalCutLinearM"] > 0
    assert "totalEdgeBandingLinearM" in data

    stats = data["layouts"][0]["statistics"]
    assert stats["cutLinearM"] > 0
    assert "edgeBandingLinearM" in stats

    total_sheets = sum(lay["statistics"]["cutLinearM"] for lay in data["layouts"])
    assert data["totalCutLinearM"] == pytest.approx(total_sheets, abs=0.02)


def test_carrier_exposes_linear_meter_totals():
    """``from_payload`` exposes the new totals; an old payload falls back to 0.0."""
    from src.modules.optimizations.carrier import ProformaCarrier

    carrier = ProformaCarrier.from_payload(
        {"total_cut_linear_m": 12.5, "total_edge_banding_linear_m": 3.2},
        client=None,
        reference="OPT-x",
    )
    assert carrier.total_cut_linear_m == 12.5
    assert carrier.total_edge_banding_linear_m == 3.2

    legacy = ProformaCarrier.from_payload({}, client=None, reference="OPT-y")
    assert legacy.total_cut_linear_m == 0.0
    assert legacy.total_edge_banding_linear_m == 0.0


def test_optimize_returns_optimization_hash(client):
    """The response exposes the deterministic hash of the inputs."""
    created_client = _create_client(client)
    created_board = _create_board(client)

    resp = client.post(
        "/api/v1/optimize/",
        json=_optimize_payload(created_client["id"], created_board["id"]),
    )
    assert resp.status_code == 200
    optimization_hash = resp.json()["data"]["optimizationHash"]
    assert isinstance(optimization_hash, str) and len(optimization_hash) == 64


def test_optimize_strategy_changes_hash_and_is_echoed(client):
    """The `strategy` heuristic affects the hash (different cache key) and is echoed.

    Omitting it is equivalent to `default`; passing `longOffcuts` produces a
    different hash so it doesn't collide in cache with the default packing.
    """
    created_client = _create_client(client)
    created_board = _create_board(client)

    base = _optimize_payload(created_client["id"], created_board["id"])
    default_resp = client.post("/api/v1/optimize/", json=base).json()["data"]

    explicit_default = client.post(
        "/api/v1/optimize/", json={**base, "strategy": "default"}
    ).json()["data"]

    long_off = client.post(
        "/api/v1/optimize/", json={**base, "strategy": "longOffcuts"}
    ).json()["data"]

    # Echo of the applied strategy.
    assert default_resp["strategy"] == "default"
    assert long_off["strategy"] == "longOffcuts"
    # Omitting == explicit default (same cache hash).
    assert explicit_default["optimizationHash"] == default_resp["optimizationHash"]
    # Different strategy => different hash (no cache collision).
    assert long_off["optimizationHash"] != default_resp["optimizationHash"]


def test_optimize_computes_total_boards_cost(client):
    """The total cost must be the number of boards used * board price."""
    created_client = _create_client(client)
    created_board = _create_board(client)

    resp = client.post(
        "/api/v1/optimize/",
        json=_full_board_payload(created_client["id"], created_board["id"]),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["totalBoardsUsed"] >= 1
    assert data["totalBoardsCost"] == pytest.approx(data["totalBoardsUsed"] * 45.5)
    assert data["totalBoardsCost"] > 0


def test_optimize_unknown_product_returns_404(client):
    created_client = _create_client(client)
    resp = client.post(
        "/api/v1/optimize/",
        json=_optimize_payload(created_client["id"], product_id=99999),
    )
    assert resp.status_code == 404
    assert "Product 99999" in resp.json()["errors"][0]["message"]


def test_optimize_non_board_product_is_rejected(client):
    """A product that is not a board cannot be optimized (422 business rule)."""
    created_client = _create_client(client)
    tapacanto = client.post(
        "/api/v1/products/",
        json={
            "type": "edge_banding",
            "code": "TAP22",
            "name": "Tapacanto PVC 22mm",
            "price": 0.8,
            "attributes": {"length": 50000, "width": 22, "thickness": 1},
        },
    ).json()["data"]

    resp = client.post(
        "/api/v1/optimize/",
        json=_optimize_payload(created_client["id"], tapacanto["id"]),
    )
    assert resp.status_code == 422
    assert "no es un tablero" in resp.json()["errors"][0]["message"].lower()


def test_optimize_without_client_is_anonymous(client):
    """``POST /optimize`` without ``clientId`` returns 200 with a null ``client``
    and the same hash as with a client (the computation is client-agnostic)."""
    created_client = _create_client(client)
    created_board = _create_board(client)

    with_client = client.post(
        "/api/v1/optimize/",
        json=_optimize_payload(created_client["id"], created_board["id"]),
    ).json()["data"]

    anon_payload = _optimize_payload(created_client["id"], created_board["id"])
    anon_payload.pop("clientId")
    anon = client.post("/api/v1/optimize/", json=anon_payload)

    assert anon.status_code == 200
    data = anon.json()["data"]
    assert data["client"] is None
    assert data["optimizationHash"] == with_client["optimizationHash"]


def test_optimize_unknown_client_returns_404(client):
    """If a nonexistent ``clientId`` is sent, the response is 404."""
    created_board = _create_board(client)
    resp = client.post(
        "/api/v1/optimize/",
        json=_optimize_payload(client_id=99999, product_id=created_board["id"]),
    )
    assert resp.status_code == 404
    assert "Client 99999" in resp.json()["errors"][0]["message"]


def test_optimize_does_not_persist(client):
    """``POST /optimize`` is cache-only: the response carries no persistent id."""
    created_client = _create_client(client)
    created_board = _create_board(client)
    resp = client.post(
        "/api/v1/optimize/",
        json=_optimize_payload(created_client["id"], created_board["id"]),
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] is None


def test_service_rejects_empty_requirements(db_session):
    """The defensive guard in ``compute`` raises ValidationError (422)."""
    request = OptimizeRequest.model_construct(requirements=[], client_id=1)
    with pytest.raises(ValidationError):
        OptimizationService(db_session).compute(request)


def test_optimize_deduplicates_identical_patterns(client):
    """Several sheets with the same pattern collapse into one group with its count."""
    created_client = _create_client(client)
    created_board = _create_board(client)

    # A large piece (1700x670) fits only once per board -> 5 identical sheets.
    payload = {
        "clientId": created_client["id"],
        "materials": [
            {"key": "b1", "source": "catalog", "productId": created_board["id"]}
        ],
        "requirements": [
            {
                "priority": 0,
                "height": 1700,
                "width": 670,
                "quantity": 5,
                "materialKey": "b1",
                "label": "",
                "canRotate": True,
            }
        ],
    }
    resp = client.post("/api/v1/optimize/", json=payload)
    assert resp.status_code == 200
    data = resp.json()["data"]

    # The physical count stays at 5 (layouts, totals, and materials summary).
    assert data["totalBoardsUsed"] == 5
    assert len(data["layouts"]) == 5
    assert data["materialsSummary"][0]["count"] == 5

    # But patterns are deduplicated into a single group with count == 5.
    groups = data["layoutGroups"]
    assert len(groups) == 1
    group = groups[0]
    assert group["patternId"] == 1
    assert group["count"] == 5
    assert len(group["sheetNumbers"]) == 5
    assert group["materialKey"] == "b1"
    assert group["layout"]["material"]["materialKey"] == "b1"


def test_optimize_includes_materials_summary(client):
    """materials_summary groups by board type with code, quantity, and cost."""
    created_client = _create_client(client)
    created_board = _create_board(client, code="MEL18")

    resp = client.post(
        "/api/v1/optimize/",
        json=_full_board_payload(created_client["id"], created_board["id"]),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]

    summary = data.get("materialsSummary")
    assert summary is not None and len(summary) == 1

    entry = summary[0]
    assert entry["materialKey"] == "b1"
    assert entry["source"] == "catalog"
    assert entry["productCode"] == "MEL18"
    assert entry["productName"] == "Melamina MEL18"
    assert entry["halfBoard"] is False
    assert entry["count"] == data["totalBoardsUsed"]
    assert entry["totalCost"] == pytest.approx(data["totalBoardsCost"])


# --------------------------------------------------------------------------- #
# Source-agnostic material (catalog / manual / offcut / mixed)
# --------------------------------------------------------------------------- #
def _manual_material(key="m1", height=2000, width=1000, thickness=18, cost=30.0):
    return {
        "key": key,
        "source": "manual",
        "height": height,
        "width": width,
        "thickness": thickness,
        "costPerUnit": cost,
        "label": "Sobrante taller",
    }


def test_optimize_with_manual_material(client):
    """A manual measurement (no catalog) is optimized by dimensions and cost."""
    c = _create_client(client)
    payload = {
        "clientId": c["id"],
        "materials": [_manual_material(cost=30.0)],
        "requirements": [
            {
                "priority": 0,
                "height": 400,
                "width": 600,
                "quantity": 2,
                "materialKey": "m1",
                "label": "Puerta",
                "canRotate": True,
            }
        ],
    }
    resp = client.post("/api/v1/optimize/", json=payload)
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert data["totalBoardsUsed"] >= 1
    assert data["layouts"][0]["material"]["materialKey"] == "m1"

    entry = data["materialsSummary"][0]
    assert entry["materialKey"] == "m1"
    assert entry["source"] == "manual"
    assert entry["productId"] is None
    assert entry["costPerUnit"] == 30.0
    assert entry["totalCost"] == pytest.approx(data["totalBoardsUsed"] * 30.0)


def test_optimize_with_company_offcut_material(client):
    """A company offcut is treated the same: inline dimensions, no product."""
    c = _create_client(client)
    payload = {
        "clientId": c["id"],
        "materials": [
            {
                "key": "r1",
                "source": "companyOffcut",
                "height": 1200,
                "width": 600,
                "thickness": 15,
                "costPerUnit": 0,
            }
        ],
        "requirements": [
            {
                "priority": 0,
                "height": 400,
                "width": 300,
                "quantity": 1,
                "materialKey": "r1",
                "canRotate": True,
            }
        ],
    }
    resp = client.post("/api/v1/optimize/", json=payload)
    assert resp.status_code == 200
    entry = resp.json()["data"]["materialsSummary"][0]
    assert entry["source"] == "companyOffcut"
    assert entry["productId"] is None
    # With no label or catalog code, it falls back to the key / readable dimensions.
    assert entry["productCode"] == "r1"
    assert entry["productName"] == "600×1200"


def test_optimize_mixed_catalog_and_manual(client):
    """Catalog and manual coexist in the same optimization (heterogeneous stock)."""
    c = _create_client(client)
    board = _create_board(client)
    payload = {
        "clientId": c["id"],
        "materials": [
            {"key": "b1", "source": "catalog", "productId": board["id"]},
            _manual_material(key="m1", cost=20.0),
        ],
        "requirements": [
            {
                "priority": 0,
                "height": 400,
                "width": 600,
                "quantity": 1,
                "materialKey": "b1",
            },
            {
                "priority": 0,
                "height": 300,
                "width": 300,
                "quantity": 1,
                "materialKey": "m1",
            },
        ],
    }
    resp = client.post("/api/v1/optimize/", json=payload)
    assert resp.status_code == 200
    data = resp.json()["data"]

    by_key = {e["materialKey"]: e for e in data["materialsSummary"]}
    assert set(by_key) == {"b1", "m1"}
    assert by_key["b1"]["source"] == "catalog"
    assert by_key["b1"]["productId"] == board["id"]
    assert by_key["m1"]["source"] == "manual"
    assert by_key["m1"]["productId"] is None


def test_optimize_unknown_material_key_returns_422(client):
    """A requirement referencing a nonexistent key is 422 (validation)."""
    c = _create_client(client)
    board = _create_board(client)
    payload = {
        "clientId": c["id"],
        "materials": [{"key": "b1", "source": "catalog", "productId": board["id"]}],
        "requirements": [
            {
                "priority": 0,
                "height": 400,
                "width": 600,
                "quantity": 1,
                "materialKey": "zzz",
            }
        ],
    }
    resp = client.post("/api/v1/optimize/", json=payload)
    assert resp.status_code == 422


def test_optimize_manual_material_hash_is_deterministic(client):
    """Two identical requests with manual material share a hash (cache-first)."""
    c = _create_client(client)
    payload = {
        "clientId": c["id"],
        "materials": [_manual_material()],
        "requirements": [
            {
                "priority": 0,
                "height": 400,
                "width": 600,
                "quantity": 1,
                "materialKey": "m1",
            }
        ],
    }
    first = client.post("/api/v1/optimize/", json=payload).json()["data"]
    second = client.post("/api/v1/optimize/", json=payload).json()["data"]
    assert first["optimizationHash"] == second["optimizationHash"]


def test_material_resolver_resolves_each_source(db_session):
    """The resolver translates catalog and inline sources into a uniform ResolvedMaterial."""
    from src.modules.optimizations.materials import MaterialResolver
    from src.modules.optimizations.schemas import (
        CatalogMaterialInput,
        InlineMaterialInput,
        MaterialSource,
    )
    from src.modules.products.model import ProductModel, ProductType

    board = ProductModel(
        type=ProductType.BOARD.value,
        code="MELX",
        name="Melamina X",
        price=50.0,
        attributes={"height": 2440, "width": 1220, "thickness": 18},
    )
    db_session.add(board)
    db_session.commit()

    resolver = MaterialResolver(db_session)

    catalog = resolver.resolve(
        CatalogMaterialInput(
            key="b1", source=MaterialSource.catalog, product_id=board.id
        )
    )
    assert catalog.is_catalog
    assert (catalog.width, catalog.height, catalog.thickness) == (1220, 2440, 18)
    assert catalog.cost_per_unit == 50.0
    assert catalog.product_id == board.id and catalog.code == "MELX"

    manual = resolver.resolve(
        InlineMaterialInput(
            key="m1",
            source=MaterialSource.manual,
            height=2000,
            width=1000,
            thickness=15,
            cost_per_unit=12.5,
            label="Sobrante",
        )
    )
    assert not manual.is_catalog
    assert manual.product_id is None and manual.code is None
    assert manual.name == "Sobrante" and manual.cost_per_unit == 12.5
    assert manual.to_dict()["source"] == "manual"


# --- Half boards (billed at half price) -----------------------------------


def _resolved_material(
    source="catalog", product_id=1, width=1220, height=2440, cost=45.5
):
    from src.modules.optimizations.materials import ResolvedMaterial

    return ResolvedMaterial(
        key="b1",
        width=width,
        height=height,
        thickness=18,
        cost_per_unit=cost,
        source=source,
        product_id=product_id,
        code="MEL18",
        name="Melamina",
    )


def _bins_for(rm, markup=0.10):
    """Full bin + half sibling (when catalog), as the service builds them."""
    from src.cutting import BinSpec
    from src.modules.optimizations.service import OptimizationService

    bins = [
        BinSpec(
            key=rm.key,
            width=rm.width,
            height=rm.height,
            thickness=rm.thickness,
            cost_per_unit=rm.cost_per_unit,
        )
    ]
    half = OptimizationService._half_spec(rm, markup)
    if half is not None:
        bins.append(half)
    return bins


def test_half_bin_downgrades_fitting_board():
    """A catalog job whose content fits in half is billed as a half board."""
    from src.cutting import CuttingParameters, Piece, optimize_bins

    layouts, unplaced = optimize_bins(
        [Piece(id="p1", width=300, height=300)],
        _bins_for(_resolved_material()),
        cutting_params=CuttingParameters(kerf=5),
    )

    assert unplaced == []
    assert len(layouts) == 1
    board = layouts[0]
    assert board.material.half_board is True
    assert board.material.width == 610  # width/2, length unchanged
    assert board.material.height == 2440
    assert board.material.cost_per_unit == 25.03  # price/2 * 1.10
    assert len(board.placed_pieces) == 1  # no pieces are lost


def test_half_bin_keeps_wide_board_full():
    """A piece wider than half (on both axes) keeps the board full."""
    from src.cutting import CuttingParameters, Piece, optimize_bins

    # 700x800: both sides > 610, doesn't fit in a half (610 wide).
    layouts, _ = optimize_bins(
        [Piece(id="p1", width=700, height=800)],
        _bins_for(_resolved_material()),
        cutting_params=CuttingParameters(kerf=5),
    )

    board = layouts[0]
    assert board.material.half_board is False
    assert board.material.width == 1220
    assert board.material.cost_per_unit == 45.5


def test_half_bin_skips_non_catalog():
    """Offcuts/manual never become half even if content fits (priced at full cost)."""
    from src.cutting import CuttingParameters, Piece, optimize_bins
    from src.modules.optimizations.service import OptimizationService

    manual = _resolved_material(source="manual", product_id=None)
    assert OptimizationService._half_spec(manual, 0.10) is None

    layouts, _ = optimize_bins(
        [Piece(id="p1", width=300, height=300)],
        _bins_for(manual),
        cutting_params=CuttingParameters(kerf=5),
    )

    board = layouts[0]
    assert board.material.half_board is False
    assert board.material.width == 1220


def test_half_bin_applies_configurable_markup():
    """The markup formula is price/2 * (1 + pct), independent of the default value."""
    from src.cutting import CuttingParameters, Piece, optimize_bins
    from src.modules.optimizations.service import OptimizationService

    half = OptimizationService._half_spec(_resolved_material(), 0.20)
    assert half.cost_per_unit == round(45.5 / 2 * 1.20, 2) == 27.3

    layouts, _ = optimize_bins(
        [Piece(id="p1", width=300, height=300)],
        _bins_for(_resolved_material(), markup=0.20),
        cutting_params=CuttingParameters(kerf=5),
    )
    assert layouts[0].material.cost_per_unit == 27.3


def test_optimize_charges_half_board_for_sparse_job(client):
    """A small catalog job is billed as a half board (price/2)."""
    created_client = _create_client(client)
    created_board = _create_board(client)  # 2440x1220, price 45.5

    payload = {
        "clientId": created_client["id"],
        "materials": [
            {"key": "b1", "source": "catalog", "productId": created_board["id"]}
        ],
        "requirements": [
            {
                "priority": 0,
                "height": 300,
                "width": 300,
                "quantity": 1,
                "materialKey": "b1",
                "label": "Repisa",
                "canRotate": True,
            }
        ],
    }
    resp = client.post("/api/v1/optimize/", json=payload)
    assert resp.status_code == 200
    data = resp.json()["data"]

    summary = data["materialsSummary"]
    assert len(summary) == 1
    assert summary[0]["halfBoard"] is True
    assert summary[0]["costPerUnit"] == 25.03  # price/2 * 1.10 (default markup)
    assert summary[0]["productName"].endswith("(medio tablero)")
    assert data["totalBoardsCost"] == 25.03

    material = data["layouts"][0]["material"]
    assert material["halfBoard"] is True
    assert material["width"] == 610


def test_optimize_keeps_full_board_for_wide_job(client):
    """A job with a wide piece is billed as a full board."""
    created_client = _create_client(client)
    created_board = _create_board(client)

    payload = {
        "clientId": created_client["id"],
        "materials": [
            {"key": "b1", "source": "catalog", "productId": created_board["id"]}
        ],
        "requirements": [
            {
                "priority": 0,
                "height": 800,
                "width": 700,
                "quantity": 1,
                "materialKey": "b1",
                "label": "Costado",
                "canRotate": True,
            }
        ],
    }
    data = client.post("/api/v1/optimize/", json=payload).json()["data"]
    summary = data["materialsSummary"]
    assert len(summary) == 1
    assert summary[0]["halfBoard"] is False
    assert summary[0]["costPerUnit"] == 45.5
    assert data["totalBoardsCost"] == 45.5


# --- Material pool: catalog board + client offcut ---------------------------


def _pool_payload(client_id, product_id, fill_order="offcutsFirst"):
    """A catalog board with an attached client offcut (same material).

    The small piece fits the 600x400 offcut; the wide one needs the catalog board.
    """
    return {
        "clientId": client_id,
        "materials": [
            {
                "key": "b1",
                "source": "catalog",
                "productId": product_id,
                "fillOrder": fill_order,
            },
            {
                "key": "off1",
                "source": "clientOffcut",
                "height": 400,
                "width": 600,
                "thickness": 18,
                "costPerUnit": 0,
                "quantity": 1,
                "poolKey": "b1",
                "label": "Retazo cliente",
            },
        ],
        "requirements": [
            {
                "priority": 0,
                "height": 200,
                "width": 300,
                "quantity": 1,
                "materialKey": "b1",
                "label": "Chico",
                "canRotate": True,
            },
            {
                "priority": 0,
                "height": 2000,
                "width": 1000,
                "quantity": 1,
                "materialKey": "b1",
                "label": "Grande",
                "canRotate": True,
            },
        ],
    }


def test_optimize_pool_places_pieces_on_offcut_and_catalog(client):
    created_client = _create_client(client)
    created_board = _create_board(client)

    resp = client.post(
        "/api/v1/optimize/",
        json=_pool_payload(created_client["id"], created_board["id"]),
    )
    assert resp.status_code == 200
    data = resp.json()["data"]

    layouts_by_key = {lay["material"]["materialKey"]: lay for lay in data["layouts"]}
    # The offcut is its own sheet, carrying the small piece.
    assert "off1" in layouts_by_key
    off_pieces = [p["pieceId"] for p in layouts_by_key["off1"]["placedPieces"]]
    assert off_pieces == ["Chico"]
    # The wide piece is on a catalog board.
    assert "Grande" in [p["pieceId"] for p in layouts_by_key["b1"]["placedPieces"]]


def test_optimize_pool_offcut_excluded_from_boards_used(client):
    created_client = _create_client(client)
    created_board = _create_board(client)

    data = client.post(
        "/api/v1/optimize/",
        json=_pool_payload(created_client["id"], created_board["id"]),
    ).json()["data"]

    # "Boards used" counts only the catalog board the client buys.
    assert data["totalBoardsUsed"] == 1
    assert data["totalBoardsCost"] == 45.5

    summary_by_key = {m["materialKey"]: m for m in data["materialsSummary"]}
    # The offcut shows up as its own $0 line, source clientOffcut, no product.
    assert summary_by_key["off1"]["source"] == "clientOffcut"
    assert summary_by_key["off1"]["totalCost"] == 0
    assert summary_by_key["off1"]["productId"] is None
    assert summary_by_key["b1"]["productId"] == created_board["id"]


def test_optimize_pool_rejects_requirement_on_pooled_offcut(client):
    created_client = _create_client(client)
    created_board = _create_board(client)

    payload = _pool_payload(created_client["id"], created_board["id"])
    # A requirement cannot target a pooled offcut directly.
    payload["requirements"][0]["materialKey"] = "off1"

    resp = client.post("/api/v1/optimize/", json=payload)
    assert resp.status_code == 422


def test_optimize_pool_rejects_pool_key_to_non_catalog(client):
    created_client = _create_client(client)
    created_board = _create_board(client)

    payload = _pool_payload(created_client["id"], created_board["id"])
    # poolKey must reference a catalog board, not another offcut.
    payload["materials"].append(
        {
            "key": "off2",
            "source": "clientOffcut",
            "height": 300,
            "width": 300,
            "thickness": 18,
            "costPerUnit": 0,
            "poolKey": "off1",
        }
    )
    resp = client.post("/api/v1/optimize/", json=payload)
    assert resp.status_code == 422


# --- Per-material process parallelism -------------------------------------
# The unit tests in tests/unit/test_optimization_parallel.py cover the plumbing.
# These cover what only the real orchestration can: that turning the feature on
# changes nothing a caller can observe.


@pytest.fixture
def release_pool():
    """A live forkserver would make pytest hang at exit."""
    yield
    parallel.shutdown_pool_executor()


def _multi_material_payload(client_id, product_ids):
    """Three materials, so the parallel path has something to overlap."""
    return {
        "clientId": client_id,
        "materials": [
            {"key": f"m{i}", "source": "catalog", "productId": pid}
            for i, pid in enumerate(product_ids)
        ],
        "requirements": [
            {
                "priority": 0,
                "height": 400 + 40 * i,
                "width": 600 + 40 * i,
                "quantity": 3 + i,
                "materialKey": f"m{i}",
                "label": f"Pieza{i}",
                "canRotate": True,
            }
            for i in range(len(product_ids))
        ],
    }


def _three_boards(client):
    return [
        _create_board(client, code=f"MEL{code}")["id"] for code in ("18", "15", "12")
    ]


def test_parallel_and_sequential_produce_identical_payloads(
    client, db_session, monkeypatch, release_pool
):
    """Turning parallelism on must change nothing: same layouts, same hash.

    The hash equality is what enforces that OPT_POOL_WORKERS stays out of
    ``_compute_hash`` — a 1-worker box and a 2-worker box must share a cache
    namespace, because they produce the same geometry.
    """
    created_client = _create_client(client)
    request = OptimizeRequest.model_validate(
        _multi_material_payload(created_client["id"], _three_boards(client))
    )
    svc = OptimizationService(db_session)

    monkeypatch.setattr(config, "OPT_POOL_WORKERS", 1)
    payload_seq, hash_seq = svc.compute(request)

    # Mandatory, and the easiest thing to get wrong: compute() is cache-first and
    # the isolated_cache fixture keeps one double per test, so without this the
    # second call returns the first call's payload and the test proves nothing
    # while staying green forever.
    cache._client._store.clear()

    monkeypatch.setattr(config, "OPT_POOL_WORKERS", 2)
    payload_par, hash_par = svc.compute(request)

    # Otherwise a broken pool would silently fall back and satisfy everything below.
    assert parallel._executor is not None and not parallel._breaker_open()
    assert hash_par == hash_seq
    assert payload_par == payload_seq


@pytest.mark.parametrize("workers", [1, 2])
def test_layout_order_follows_material_declaration_order(
    client, db_session, monkeypatch, release_pool, workers
):
    """``_build_result_payload`` flattens results in order; parallelism must not
    reorder them just because a small material finished first."""
    monkeypatch.setattr(config, "OPT_POOL_WORKERS", workers)
    created_client = _create_client(client)
    request = OptimizeRequest.model_validate(
        _multi_material_payload(created_client["id"], _three_boards(client))
    )

    payload, _ = OptimizationService(db_session).compute(request)

    grouped = []
    for layout in payload["layouts"]:
        key = layout["material"]["material_key"]
        if not grouped or grouped[-1] != key:
            grouped.append(key)
    assert grouped == ["m0", "m1", "m2"]


def test_optimize_endpoint_works_with_parallelism_enabled(
    client, monkeypatch, release_pool
):
    """One end-to-end run through the real FastAPI stack with workers on.

    The route handler is a sync ``def``, so it executes on an anyio threadpool
    thread — which is exactly where a fork-from-a-thread problem would surface.
    """
    monkeypatch.setattr(config, "OPT_POOL_WORKERS", 2)
    created_client = _create_client(client)
    payload = _multi_material_payload(created_client["id"], _three_boards(client))

    resp = client.post("/api/v1/optimize/", json=payload)

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert {m["materialKey"] for m in data["materialsSummary"]} == {"m0", "m1", "m2"}
    assert data["totalBoardsUsed"] >= 3
