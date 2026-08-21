"""Tests for edge banding: linear meters, validation, rotation rule and order charges."""

import pytest

from src.modules.optimizations.labels import edge_banding_notation
from src.modules.optimizations.schemas import EdgeBandingSpec, EdgeSide
from src.modules.optimizations.service import OptimizationService
from src.modules.orders.schemas import OrderCreate
from src.modules.orders.service import OrderService
from src.shared.config import config


def _create_client(client, identifier="0991112233", phone="0991112233"):
    return client.post(
        "/api/v1/clients/",
        json={
            "identifier": identifier,
            "firstName": "Ada",
            "lastName": "Lovelace",
            "phone": phone,
        },
    ).json()["data"]


def _create_board(client, code="MEL18", price=45.5):
    return client.post(
        "/api/v1/products/",
        json={
            "type": "board",
            "code": code,
            "name": f"Melamina {code}",
            "price": price,
            "attributes": {"height": 2440, "width": 1220, "thickness": 18},
        },
    ).json()["data"]


def _create_edge_banding(
    client,
    code="TAP22",
    price=2.0,
    color="Blanco",
    band_type=None,
    family=None,
    alias=None,
):
    attributes = {"thickness": 0.45, "width": 22, "color": color, "length": 50000}
    if band_type is not None:
        attributes["band_type"] = band_type
    if family is not None:
        attributes["family"] = family
    if alias is not None:
        attributes["alias"] = alias
    return client.post(
        "/api/v1/products/",
        json={
            "type": "edge_banding",
            "code": code,
            "name": f"Tapacanto {code}",
            "price": price,
            "attributes": attributes,
        },
    ).json()["data"]


def _materials(board_id, key="b1"):
    """Catalog stock: one board referenced by ``materialKey``."""
    return [{"key": key, "source": "catalog", "productId": board_id}]


def _requirement(eb_id, sides, height=500, width=1000, quantity=1, material_key="b1"):
    return {
        "priority": 0,
        "height": height,
        "width": width,
        "quantity": quantity,
        "materialKey": material_key,
        "label": "Costado",
        "canRotate": True,
        "edgeBanding": {"productId": eb_id, "sides": sides},
    }


def _expected_meters(net_m: float):
    """Mirrors the service's formula: net + waste factor, billed exactly (no rounding)."""
    billed = round(net_m * (1 + config.EDGE_BANDING_WASTE_FACTOR), 2)
    return billed, billed


# --------------------------------------------------------------------------- #
# Workshop notation for edges (2L1C CS)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sides, band_type, expected",
    [
        (["left", "right", "top"], "Soft", "2L1C CS"),  # 2 long (height) + 1 short
        (["left"], "Soft", "1L CS"),  # only long: omits the zero part
        (["top", "bottom"], "Hard", "2C CD"),  # only short (width), hard edge
        (["left", "right", "top", "bottom"], "Soft", "4L CS"),  # all sides
        ([], "Soft", ""),  # no sides → empty
        (["left", "right"], None, "2L"),  # no band_type → no suffix
    ],
)
def test_edge_banding_notation(sides, band_type, expected):
    assert edge_banding_notation(sides, band_type) == expected


@pytest.mark.parametrize(
    "sides, band_type, alias, expected",
    [
        (["left", "right", "top"], "Soft", "CSH", "2L1C CS CSH"),
        (["left"], "Soft", "CSH", "1L CS CSH"),
        (["top", "bottom"], "Hard", "BRD", "2C CD BRD"),
        (["left", "right", "top", "bottom"], "Soft", "BLN", "4L CS BLN"),
        # An alias without a band type still shows: they're independent.
        (["left", "right"], None, "CSH", "2L CSH"),
        # Uppercased for the tag.
        (["left"], "Soft", " csh ", "1L CS CSH"),
        # No sides = nothing to qualify; the suffixes are dropped too.
        ([], "Soft", "CSH", ""),
        # A banding with no alias configured renders exactly as before.
        (["left"], "Soft", None, "1L CS"),
        (["left"], "Soft", "  ", "1L CS"),
    ],
)
def test_edge_banding_notation_with_alias(sides, band_type, alias, expected):
    assert edge_banding_notation(sides, band_type, alias) == expected


# --------------------------------------------------------------------------- #
# Linear meters and cost (/optimize endpoint)
# --------------------------------------------------------------------------- #
def test_optimize_aggregates_edge_banding_meters_and_cost(client):
    c = _create_client(client)
    b = _create_board(client)
    eb = _create_edge_banding(client, price=2.0)

    # height=500, width=1000; top+bottom sides = 2×width = 2000 mm.
    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": [_requirement(eb["id"], ["top", "bottom"])],
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]

    summary = data["edgeBandingsSummary"]
    assert summary is not None and len(summary) == 1
    entry = summary[0]
    assert entry["productCode"] == "TAP22"
    assert entry["color"] == "Blanco"
    assert entry["netLinearM"] == pytest.approx(2.0)

    linear_m, billed = _expected_meters(2.0)
    assert entry["linearM"] == pytest.approx(linear_m)
    assert entry["billedLinearM"] == billed
    assert entry["pricePerM"] == 2.0
    assert entry["totalCost"] == pytest.approx(round(billed * 2.0, 2))
    assert data["totalEdgeBandingCost"] == pytest.approx(round(billed * 2.0, 2))


def test_optimize_reports_edge_banding_linear_m_per_sheet(client):
    """The net edge linear meters is exposed per sheet and as the overall total."""
    c = _create_client(client)
    b = _create_board(client)
    eb = _create_edge_banding(client, price=2.0)

    # top+bottom with width=1000 → 2×1000 = 2000 mm = 2.0 net m (1 piece, 1 sheet).
    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": [_requirement(eb["id"], ["top", "bottom"])],
        },
    )
    data = resp.json()["data"]
    assert data["totalEdgeBandingLinearM"] == pytest.approx(2.0)
    stats = data["layouts"][0]["statistics"]
    assert stats["edgeBandingLinearM"] == pytest.approx(2.0)


def test_unlabeled_piece_still_reports_edge_banding_per_sheet(client):
    """Regression: a piece WITHOUT a label and ``quantity=1`` uses the auto-label
    ``piece_1``; the per-sheet linear meters and the piece's ``edges`` must not be
    lost (previously ``base_label`` mangled the ``_1`` and produced 0)."""
    c = _create_client(client)
    b = _create_board(client)
    eb = _create_edge_banding(client, price=2.0)

    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": [
                {
                    "priority": 0,
                    "height": 700,
                    "width": 1000,
                    "quantity": 1,
                    "materialKey": "b1",
                    "canRotate": True,
                    "edgeBanding": {"productId": eb["id"], "sides": ["top", "bottom"]},
                }
            ],
        },
    )
    data = resp.json()["data"]
    # top+bottom with width=1000 → 2×1000 = 2000 mm = 2.0 net m.
    assert data["totalEdgeBandingLinearM"] == pytest.approx(2.0)
    layout = data["layouts"][0]
    assert layout["statistics"]["edgeBandingLinearM"] == pytest.approx(2.0)
    # Edge banding propagates to the placed piece (for the diagram / shop sheet).
    placed = layout["placedPieces"][0]
    assert placed["edges"] is not None
    assert placed["edges"]["sides"] == ["top", "bottom"]


def test_optimize_edge_banding_without_product_reports_length_only(client):
    """Geometry-only edge banding: ``edgeBanding`` with sides but WITHOUT ``productId``
    computes the edge length (what the optimizer cares about) without resolving or
    charging a product; that's only assigned when quoting."""
    c = _create_client(client)
    b = _create_board(client)

    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": [
                {
                    "priority": 0,
                    "height": 700,
                    "width": 1000,
                    "quantity": 1,
                    "materialKey": "b1",
                    "canRotate": True,
                    "edgeBanding": {"sides": ["top", "bottom"]},
                }
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    # The length is still computed: top+bottom with width=1000 → 2.0 net m.
    assert data["totalEdgeBandingLinearM"] == pytest.approx(2.0)
    # No product: no edge cost and the summary has no identity or price.
    assert data["totalEdgeBandingCost"] == 0.0
    entry = data["edgeBandingsSummary"][0]
    assert entry["productId"] is None
    assert entry["productCode"] is None
    assert entry["pricePerM"] == 0.0
    assert entry["netLinearM"] == pytest.approx(2.0)
    # Edge banding propagates to the placed piece (for the diagram) with no product.
    # ``edges`` is a raw dict (not a Pydantic model): its keys are snake_case.
    placed = data["layouts"][0]["placedPieces"][0]
    assert placed["edges"]["sides"] == ["top", "bottom"]
    assert placed["edges"]["product_id"] is None


def test_same_label_pieces_keep_their_own_edge_banding(client):
    """Regression: several pieces with the SAME label and ``quantity=1`` but different
    edge banding must NOT collapse. Edge banding used to be indexed by label, so all
    of them inherited the last spec (all 4 sides) and the per-sheet linear meters came
    out inflated; now each placed piece keeps its own sides thanks to the unique id."""
    c = _create_client(client)
    b = _create_board(client)
    eb = _create_edge_banding(client, price=2.0)

    # Same label "Puerta", different width per piece (geometric identity) and
    # different edge banding; canRotate=False so the geometric side == nominal.
    sides_by_width = {
        300: {"left"},
        301: {"left", "right"},
        302: {"top"},
        303: {"top", "bottom", "left", "right"},
    }
    requirements = [
        {
            "priority": 0,
            "height": 500,
            "width": w,
            "quantity": 1,
            "materialKey": "b1",
            "label": "Puerta",
            "canRotate": False,
            "edgeBanding": {"productId": eb["id"], "sides": list(sides)},
        }
        for w, sides in sides_by_width.items()
    ]
    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": requirements,
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]

    # Each placed piece keeps ITS sides, not the 4 from the last requirement.
    placed = data["layouts"][0]["placedPieces"]
    assert len(placed) == len(sides_by_width)
    got = {p["originalWidth"]: set(p["edges"]["sides"]) for p in placed}
    assert got == sides_by_width

    # The per-sheet linear meters matches the summary's net (not inflated ×#pieces).
    # net per piece: 500 + 1000 + 302 + (303+303+500+500) = 3408 mm = 3.41 m.
    net_summary = data["edgeBandingsSummary"][0]["netLinearM"]
    assert net_summary == pytest.approx(3.41)
    stats = data["layouts"][0]["statistics"]
    assert stats["edgeBandingLinearM"] == pytest.approx(net_summary)
    assert data["totalEdgeBandingLinearM"] == pytest.approx(net_summary)


def test_optimize_uses_height_for_left_right_sides(client):
    """left/right measure the height; top/bottom the width."""
    c = _create_client(client)
    b = _create_board(client)
    eb = _create_edge_banding(client, price=1.0)

    # height=500: left+right = 2×500 = 1000 mm = 1.0 net m.
    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": [_requirement(eb["id"], ["left", "right"])],
        },
    )
    data = resp.json()["data"]
    assert data["edgeBandingsSummary"][0]["netLinearM"] == pytest.approx(1.0)


def test_optimize_aggregates_multiple_edge_banding_types(client):
    c = _create_client(client)
    b = _create_board(client)
    eb1 = _create_edge_banding(client, code="TAP22", price=2.0)
    eb2 = _create_edge_banding(client, code="TAP15", price=3.0, color="Nogal")

    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": [
                _requirement(eb1["id"], ["top", "bottom"]),
                _requirement(eb2["id"], ["left", "right"], height=1000),
            ],
        },
    )
    data = resp.json()["data"]
    summary = {e["productCode"]: e for e in data["edgeBandingsSummary"]}
    assert set(summary) == {"TAP22", "TAP15"}
    total = sum(e["totalCost"] for e in data["edgeBandingsSummary"])
    assert data["totalEdgeBandingCost"] == pytest.approx(round(total, 2))


def test_optimize_without_edge_banding_has_empty_summary(client):
    c = _create_client(client)
    b = _create_board(client)
    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": [
                {
                    "priority": 0,
                    "height": 400,
                    "width": 600,
                    "quantity": 1,
                    "materialKey": "b1",
                    "label": "P",
                    "canRotate": True,
                }
            ],
        },
    )
    data = resp.json()["data"]
    assert data["totalEdgeBandingCost"] == 0.0
    assert data["edgeBandingsSummary"] in (None, [])


# --------------------------------------------------------------------------- #
# Edge-banding product validation
# --------------------------------------------------------------------------- #
def test_edge_banding_unknown_product_returns_404(client):
    c = _create_client(client)
    b = _create_board(client)
    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": [_requirement(99999, ["top"])],
        },
    )
    assert resp.status_code == 404
    assert "Product 99999" in resp.json()["errors"][0]["message"]


def test_edge_banding_non_edge_banding_product_rejected(client):
    """If edgeBanding.productId points to a board → business rule 422."""
    c = _create_client(client)
    b = _create_board(client)
    other_board = _create_board(client, code="MEL15")
    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": [_requirement(other_board["id"], ["top"])],
        },
    )
    assert resp.status_code == 422
    assert "no es un tapacanto" in resp.json()["errors"][0]["message"].lower()


def test_edge_banding_rejects_duplicate_sides(client):
    c = _create_client(client)
    b = _create_board(client)
    eb = _create_edge_banding(client)
    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": [_requirement(eb["id"], ["top", "top"])],
        },
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Geometric sides and remapping on rotation
# --------------------------------------------------------------------------- #
def test_geometric_edges_mapping(db_session):
    svc = OptimizationService(db_session)
    spec_tb = EdgeBandingSpec(product_id=2, sides=[EdgeSide.top, EdgeSide.bottom])
    spec_lr = EdgeBandingSpec(product_id=2, sides=[EdgeSide.left, EdgeSide.right])
    spec_asym = EdgeBandingSpec(
        product_id=2, sides=[EdgeSide.top, EdgeSide.left, EdgeSide.right]
    )

    # Not rotated: identity.
    assert svc._geometric_edges(spec_tb, {}, False)["sides"] == ["top", "bottom"]
    assert svc._geometric_edges(spec_asym, {}, False)["sides"] == [
        "top",
        "left",
        "right",
    ]
    # Rotated (90° clockwise): top→right, right→bottom, bottom→left, left→top.
    assert svc._geometric_edges(spec_tb, {}, True)["sides"] == ["left", "right"]
    assert svc._geometric_edges(spec_lr, {}, True)["sides"] == ["top", "bottom"]
    # Asymmetric is also remapped (rotation isn't blocked).
    assert svc._geometric_edges(spec_asym, {}, True)["sides"] == [
        "top",
        "bottom",
        "right",
    ]


def test_geometric_edges_notation_is_rotation_invariant(db_session):
    """The notation is computed from the NOMINAL sides, so it doesn't change on rotation."""
    svc = OptimizationService(db_session)
    # top+left+right: 2 long (left/right=height) + 1 short (top=width). No product
    # in the map → no band_type → no CS/CD suffix.
    spec = EdgeBandingSpec(
        product_id=2, sides=[EdgeSide.top, EdgeSide.left, EdgeSide.right]
    )
    assert svc._geometric_edges(spec, {}, False)["notation"] == "2L1C"
    assert svc._geometric_edges(spec, {}, True)["notation"] == "2L1C"


def test_geometric_edges_keep_the_nominal_sides(db_session):
    """``nominal_sides`` is the frame the measurements and the notation refer to.

    It's what a consumer needs to describe the piece as it was ordered, so it
    must survive rotation untouched while ``sides`` moves.
    """
    svc = OptimizationService(db_session)
    spec = EdgeBandingSpec(
        product_id=2, sides=[EdgeSide.top, EdgeSide.left, EdgeSide.right]
    )
    nominal = ["top", "left", "right"]

    straight = svc._geometric_edges(spec, {}, False)
    assert straight["nominal_sides"] == nominal
    assert straight["sides"] == nominal  # not rotated: both frames coincide

    turned = svc._geometric_edges(spec, {}, True)
    assert turned["nominal_sides"] == nominal
    assert turned["sides"] == ["top", "bottom", "right"]


def test_asymmetric_banding_rotates_and_swaps_edges(client):
    """Asymmetric edge banding does NOT block rotation: if the piece ends up
    rotated, the edges swap to the corresponding physical side."""
    c = _create_client(client)
    b = _create_board(client)  # 2440 (height) × 1220 (width)
    eb = _create_edge_banding(client)

    # Piece 1000(height) × 2000(width): only fits rotated (width 2000 > board 1220).
    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": [
                _requirement(eb["id"], ["top", "left"], height=1000, width=2000)
            ],
        },
    )
    placed = resp.json()["data"]["layouts"][0]["placedPieces"][0]
    assert placed["rotated"] is True
    # CW: top→right, left→top ⇒ geometric sides {top, right}.
    assert placed["edges"]["sides"] == ["top", "right"]
    assert placed["edges"]["code"] == "TAP22"


def test_placed_piece_notation_includes_band_type(client):
    """The placed piece carries the edge notation with the CS/CD suffix from band_type."""
    c = _create_client(client)
    b = _create_board(client)
    eb = _create_edge_banding(client, band_type="Soft")

    # height=500, width=1000, not rotated: left+right = 2 long, top = 1 short; Soft → CS.
    req = _requirement(eb["id"], ["left", "right", "top"])
    req["canRotate"] = False
    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": [req],
        },
    )
    placed = resp.json()["data"]["layouts"][0]["placedPieces"][0]
    assert placed["rotated"] is False
    # The fixture configures no alias, so only the CS suffix rides along.
    assert placed["edges"]["notation"] == "2L1C CS"
    # The canonical type travels with the placed piece (soft/hard distinction in the diagram).
    # ``edges`` is a raw dict → the key is serialized as-is (snake_case).
    assert placed["edges"]["band_type"] == "Soft"


def test_alias_is_frozen_across_the_optimize_payload(client):
    """The alias reaches every consumer of the payload, not just one.

    The PDFs render from the frozen payload with no DB session, so the product's
    ``alias`` has to be denormalized at compute time — in the placed piece
    (diagram + thermal labels) and in the banding summary. ``family`` stays
    unset here on purpose: coordination isn't under test, only the printed alias.
    """
    c = _create_client(client)
    b = _create_board(client)
    eb = _create_edge_banding(client, band_type="Soft", alias="CSH")

    req = _requirement(eb["id"], ["left", "right", "top"])
    req["canRotate"] = False
    resp = client.post(
        "/api/v1/optimize/",
        json={
            "clientId": c["id"],
            "materials": _materials(b["id"]),
            "requirements": [req],
        },
    )
    data = resp.json()["data"]

    placed = data["layouts"][0]["placedPieces"][0]
    assert placed["edges"]["notation"] == "2L1C CS CSH"
    assert placed["edges"]["alias"] == "CSH"
    assert data["edgeBandingsSummary"][0]["alias"] == "CSH"


def test_requirement_dump_carries_the_alias(client, db_session):
    """The ``Cantos`` column reads ``requirements[].edge_banding``, not the layouts."""
    c = _create_client(client)
    b = _create_board(client)
    eb = _create_edge_banding(client, band_type="Soft", alias="CSH")

    order = OrderService(db_session).create(
        OrderCreate.model_validate(
            {
                "clientId": c["id"],
                "branchId": 1,
                "materials": _materials(b["id"]),
                "requirements": [_requirement(eb["id"], ["left"])],
            }
        )
    )
    spec = order.optimization_snapshot["requirements"][0]["edge_banding"]
    assert spec["band_type"] == "Soft"
    assert spec["alias"] == "CSH"


# --------------------------------------------------------------------------- #
# Durable charge on orders
# --------------------------------------------------------------------------- #
def _mint_order(client, db_session, payload):
    """Mints an order via the service (HTTP creation was removed) and reads it back via GET."""
    order = OrderService(db_session).create(OrderCreate.model_validate(payload))
    return client.get(f"/api/v1/orders/{order.id}").json()["data"]


def test_order_charges_edge_banding(client, db_session):
    c = _create_client(client)
    b = _create_board(client, price=45.5)
    eb = _create_edge_banding(client, price=2.0)

    data = _mint_order(
        client,
        db_session,
        {
            "clientId": c["id"],
            "branchId": 1,
            "materials": _materials(b["id"]),
            "requirements": [_requirement(eb["id"], ["top", "bottom"])],
        },
    )

    # Two charge lines: board + edge banding.
    lines = {line["productCode"]: line for line in data["lines"]}
    assert "MEL18" in lines and "TAP22" in lines

    _, billed = _expected_meters(2.0)
    eb_line = lines["TAP22"]
    assert eb_line["quantity"] == billed
    assert eb_line["unitPriceSnapshot"] == 2.0
    assert eb_line["lineTotal"] == pytest.approx(round(billed * 2.0, 2))
    assert eb_line["linearM"] is not None

    # Immutable totals = boards + edge bandings.
    expected_total = round(sum(line["lineTotal"] for line in data["lines"]), 2)
    assert data["subtotal"] == data["total"] == pytest.approx(expected_total)

    # The piece freezes its edge banding (nominal sides).
    assert data["pieces"][0]["edges"]["sides"] == ["top", "bottom"]

    # The billing export includes the edge-banding line.
    exported = client.get(f"/api/v1/orders/{data['id']}/export").json()["data"]
    codes = {line["productCode"] for line in exported["lines"]}
    assert {"MEL18", "TAP22"} <= codes


def test_order_document_with_edge_banding_renders(client, db_session):
    c = _create_client(client)
    b = _create_board(client)
    eb = _create_edge_banding(client)
    order = _mint_order(
        client,
        db_session,
        {
            "clientId": c["id"],
            "branchId": 1,
            "materials": _materials(b["id"]),
            "requirements": [_requirement(eb["id"], ["top", "bottom"])],
        },
    )

    document = client.get(f"/api/v1/orders/{order['id']}/document")
    assert document.status_code == 200
    assert len(document.content) > 1000

    sheet = client.get(f"/api/v1/orders/{order['id']}/production-sheet")
    assert sheet.status_code == 200
    assert len(sheet.content) > 1000


def test_production_sheet_renders_soft_and_hard_bands(client, db_session):
    """The production sheet (B/W) renders pieces with soft and hard edges: exercises
    the hard-edge hatching and both legend entries. The summary exposes bandType."""
    c = _create_client(client)
    b = _create_board(client)
    soft = _create_edge_banding(client, code="TAP-SOFT", price=2.0, band_type="Soft")
    hard = _create_edge_banding(client, code="TAP-HARD", price=3.0, band_type="Hard")

    payload = {
        "clientId": c["id"],
        "branchId": 1,
        "materials": _materials(b["id"]),
        "requirements": [
            _requirement(soft["id"], ["top", "bottom"]),
            _requirement(hard["id"], ["left", "right"]),
        ],
    }

    # The edge-banding summary exposes the type (Pydantic field → camelCase ``bandType``).
    summary = client.post("/api/v1/optimize/", json=payload).json()["data"][
        "edgeBandingsSummary"
    ]
    by_code = {entry["productCode"]: entry for entry in summary}
    assert by_code["TAP-SOFT"]["bandType"] == "Soft"
    assert by_code["TAP-HARD"]["bandType"] == "Hard"

    order = _mint_order(client, db_session, payload)
    sheet = client.get(f"/api/v1/orders/{order['id']}/production-sheet")
    assert sheet.status_code == 200
    assert len(sheet.content) > 1000
