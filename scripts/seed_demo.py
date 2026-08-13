"""Demo seed: branches, users, clients, pre-orders, orders and analytics history.

Generates a complete, realistic dataset to populate the dashboard / do QA:

- **Branches**: taken from ``COMPANY_BRANCHES`` in the environment (.env): Sucúa and Macas.
- **Users**: 1 global admin + 1 seller, 1 operator and 1 bander per branch
  (``admin@empresa.com``, ``vendedor<slug>@empresa.com``, ``operador<slug>@empresa.com``,
  ``canteador<slug>@empresa.com``).
- **Clients**: 5, all with a phone number (required to issue a proforma/order).
- **Boards and edge bandings**: reuses the catalog if present; otherwise creates
  coordinated demo boards + edge bandings (optimizer input + banding track).
- **Pre-orders**: one in EVERY status per branch (draft, sent, changes_requested,
  confirmed, rejected, expired, cancelled), walking the real flow (review link
  + client actions) where applicable.
- **Orders**: one in EVERY status per branch (confirmed, queued, cutting,
  cut, completed, cancelled), advancing the state machine with the correct
  actors and roles (the operator self-assigns on ``cutting``, all pieces are
  marked cut before closing the cut, etc.). Workshop orders
  (cutting/cut/completed) include edge banding and demonstrate the banding
  track with each branch's bander.
- **Analytics history**: extra orders per status per branch, driven through the
  same state machine and then backdated (order/history/piece/banding timestamps)
  over the last ~2 months with randomized stage durations, so ``/analytics/bottlenecks``
  and ``/analytics/users`` have enough spread to be meaningful (not everything
  happening "now"). Plus synthetic login events for every seeded user over the
  last ``ATTENDANCE_DAYS`` business days, to populate ``/analytics/attendance``.

Idempotent for the foundations (branches/users/clients/boards: get-or-create).
Pre-orders/orders/login events are only generated if no demo data exists yet
(flagged with ``source="seed"``); use ``--reset`` to delete and regenerate them.

Requires the schema to already exist (migrations applied). Examples:

    make seed-demo
    DATABASE_URL=postgresql://cutter:cutter@localhost:5433/cutter_db \\
        .venv/bin/python scripts/seed_demo.py [--reset]
"""

import argparse
import itertools
import os
import random
import sys
import unicodedata

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load .env BEFORE importing config (environs doesn't read it on its own). This way
# branches (COMPANY_BRANCHES) and other settings come from .env. ``override=False``:
# a variable already present in the environment (e.g. DATABASE_URL passed by the
# make target pointing at the local Postgres) wins over the one in the file.
from environs import Env  # noqa: E402

Env().read_env(os.path.join(os.path.dirname(__file__), "..", ".env"), recurse=False)

from datetime import datetime, timedelta, timezone  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from sqlalchemy import or_  # noqa: E402

from src.modules.branches.model import BranchModel  # noqa: E402
from src.modules.clients.model import ClientModel  # noqa: E402
from src.modules.orders.model import (  # noqa: E402
    BandingStatus,
    OrderModel,
    OrderStatus,
)
from src.modules.orders.schemas import OrderCreate, OrderPaymentInput  # noqa: E402
from src.modules.orders.service import OrderService  # noqa: E402
from src.modules.preorders.model import (  # noqa: E402
    PreOrderModel,
    PreOrderStatus,
)
from src.modules.preorders.review_service import PreOrderReviewService  # noqa: E402
from src.modules.preorders.schemas import PreOrderCreate  # noqa: E402
from src.modules.preorders.service import PreOrderService  # noqa: E402
from src.modules.products.model import ProductModel, ProductType  # noqa: E402
from src.modules.users.enums import UserRole  # noqa: E402
from src.modules.users.login_event_model import UserLoginEventModel  # noqa: E402
from src.modules.users.model import UserModel  # noqa: E402
from src.shared.audit import staff_actor  # noqa: E402
from src.shared.config import config  # noqa: E402
from src.shared.database import SessionLocal  # noqa: E402
from src.shared.security import hash_password  # noqa: E402

# Fixed seed: the demo dataset is reproducible across runs/environments.
random.seed(20260701)

# Single password for all seeded users (override with SEED_PASSWORD).
SEED_PASSWORD = os.getenv("SEED_PASSWORD") or config.ADMIN_PASSWORD or "Cutter2026!"

# Origin marker to identify (and be able to reset) the demo transactional data.
SEED_SOURCE = "seed"

# 5 clients: (identifier, first name, last name, phone, email).
CLIENTS = [
    ("0102030405", "María", "González", "0991111111", "maria.gonzalez@example.com"),
    ("0203040506", "Carlos", "Pérez", "0992222222", "carlos.perez@example.com"),
    ("0304050607", "Lucía", "Vásquez", "0993333333", "lucia.vasquez@example.com"),
    ("0405060708", "Jorge", "Mendoza", "0994444444", None),
    ("0506070809", "Ana", "Torres", "0995555555", "ana.torres@example.com"),
]

# Demo boards (only created if the catalog is empty): (code, name, height, width,
# thickness, price, family). Large dimensions so the demo cutlists fit comfortably.
# ``family`` is the shared design key that coordinates a board with its tapacanto,
# and a tapacanto's family is also printed in the workshop notation ("1L CS BLN"),
# so it's a short design code rather than a readable name.
DEMO_BOARDS = [
    ("D-BLN-15", "Tablero Demo Blanco 15mm", 2440, 1830, 15, 48.00, "BLN"),
    ("D-NGR-15", "Tablero Demo Negro 15mm", 2440, 1830, 15, 52.00, "NGR"),
    ("D-ROB-18", "Tablero Demo Roble 18mm", 2440, 1830, 18, 64.00, "ROB"),
]

# Demo edge bandings coordinated with the first two boards by shared ``family``
# (15mm board → 19mm tapacanto): (code, name, thickness_mm, width_mm, band_type, price_per_m, family).
DEMO_EDGE_BANDINGS = [
    ("D-BLN-C045", "Tapacanto Demo Blanco 19mm", 0.45, 19, "Soft", 0.35, "BLN"),
    ("D-NGR-C045", "Tapacanto Demo Negro 19mm", 0.45, 19, "Soft", 0.35, "NGR"),
]

# Statuses to seed per branch.
PREORDER_STATUSES = [
    PreOrderStatus.draft,
    PreOrderStatus.sent,
    PreOrderStatus.changes_requested,
    PreOrderStatus.confirmed,
    PreOrderStatus.rejected,
    PreOrderStatus.expired,
    PreOrderStatus.cancelled,
]
ORDER_STATUSES = [
    OrderStatus.confirmed,
    OrderStatus.queued,
    OrderStatus.cutting,
    OrderStatus.cut,
    OrderStatus.completed,
    OrderStatus.dispatched,
    OrderStatus.cancelled,
]

# How many extra backdated orders to generate per status, per branch, to give
# /analytics/bottlenecks and /analytics/users enough volume/spread to be meaningful.
ORDERS_PER_STATUS_ANALYTICS = 6

# How far back (in days) an order in a given FINAL status is placed, before
# walking backward through its stage durations. Kept within ~2 months so the
# order code's year (stamped from the real creation instant) never drifts.
FINAL_AGE_DAYS_RANGE = {
    OrderStatus.confirmed: (0, 2),
    OrderStatus.queued: (0, 4),
    OrderStatus.cutting: (0, 6),
    OrderStatus.cut: (1, 10),
    OrderStatus.completed: (3, 30),
    OrderStatus.dispatched: (5, 55),
    OrderStatus.cancelled: (0, 15),
}

# Randomized duration (hours) of each stage transition; keys name the stage
# being ENTERED (e.g. "cutting" = queued -> cutting).
STAGE_DURATION_HOURS_RANGE = {
    "queued": (1, 24),  # confirmed -> queued
    "cutting": (1, 48),  # queued -> cutting
    "cut": (1, 12),  # cutting -> cut (actual cutting time)
    "completed": (1, 72),  # cut -> completed
    "dispatched": (1, 48),  # completed -> dispatched
    "cancelled": (1, 72),  # confirmed -> cancelled
}

# Business days of synthetic login history for /analytics/attendance.
ATTENDANCE_DAYS = 45

# Approximate clock-in hour per role (24h, fractional, LOCAL time), jittered per day.
ROLE_BASE_HOUR = {
    UserRole.ADMIN.value: 8.5,
    UserRole.SELLER.value: 8.75,
    UserRole.OPERATOR.value: 7.5,
    UserRole.BANDER.value: 7.75,
}

# Everything else in the app (utcnow()) stores naive UTC; login events are the
# only seed data with an absolute "wall-clock hour" meaning, so they need an
# explicit local -> UTC conversion (the dashboard renders them in this zone).
LOCAL_TZ = ZoneInfo(config.DEFAULT_TIMEZONE)

# Global counter: guarantees a unique optimization hash per entity (via the
# pieces' ``label``), so order dedupe never collapses them.
_seq = itertools.count(1)


def _stage_hours(key: str) -> timedelta:
    lo, hi = STAGE_DURATION_HOURS_RANGE[key]
    return timedelta(hours=random.uniform(lo, hi))


def build_order_timeline(target_status: OrderStatus) -> dict[str, datetime]:
    """Backdated timestamp per stage reached on the way to ``target_status``.

    Walks BACKWARD from a randomized "final age" so every timestamp lands
    safely in the past (never after ``utcnow()``), then derives earlier stages
    by subtracting randomized stage durations. Keys are stage names
    (``created``/``queued``/``cutting``/``cut``/``completed``/``dispatched``/
    ``cancelled``); only the stages actually reached are present.
    """
    lo, hi = FINAL_AGE_DAYS_RANGE[target_status]
    final_ts = datetime.utcnow() - timedelta(days=random.uniform(lo, hi))

    if target_status == OrderStatus.confirmed:
        timeline = {"created": final_ts}
    elif target_status == OrderStatus.cancelled:
        timeline = {
            "created": final_ts - _stage_hours("cancelled"),
            "cancelled": final_ts,
        }
    elif target_status == OrderStatus.queued:
        timeline = {"created": final_ts - _stage_hours("queued"), "queued": final_ts}
    elif target_status == OrderStatus.cutting:
        queued_ts = final_ts - _stage_hours("cutting")
        timeline = {
            "created": queued_ts - _stage_hours("queued"),
            "queued": queued_ts,
            "cutting": final_ts,
        }
    elif target_status == OrderStatus.cut:
        cutting_ts = final_ts - _stage_hours("cut")
        queued_ts = cutting_ts - _stage_hours("cutting")
        timeline = {
            "created": queued_ts - _stage_hours("queued"),
            "queued": queued_ts,
            "cutting": cutting_ts,
            "cut": final_ts,
        }
    elif target_status == OrderStatus.completed:
        cut_ts = final_ts - _stage_hours("completed")
        cutting_ts = cut_ts - _stage_hours("cut")
        queued_ts = cutting_ts - _stage_hours("cutting")
        timeline = {
            "created": queued_ts - _stage_hours("queued"),
            "queued": queued_ts,
            "cutting": cutting_ts,
            "cut": cut_ts,
            "completed": final_ts,
        }
    else:  # dispatched
        completed_ts = final_ts - _stage_hours("dispatched")
        cut_ts = completed_ts - _stage_hours("completed")
        cutting_ts = cut_ts - _stage_hours("cut")
        queued_ts = cutting_ts - _stage_hours("cutting")
        timeline = {
            "created": queued_ts - _stage_hours("queued"),
            "queued": queued_ts,
            "cutting": cutting_ts,
            "cut": cut_ts,
            "completed": completed_ts,
            # Key must match OrderStatus.dispatched.value ("despachado"), not
            # the enum member name, since it's matched against history.to_status.
            OrderStatus.dispatched.value: final_ts,
        }

    # The initial history row (None -> confirmed) always matches "created": every
    # order is born 'confirmed', regardless of how far it advanced from there.
    timeline["confirmed"] = timeline["created"]
    return timeline


def backdate_order(db, order, timeline: dict[str, datetime], has_banding: bool) -> None:
    """Rewrites an already-driven order's timestamps to match ``timeline``.

    Spreads piece ``cut_at`` across the ``[cutting, cut]`` window (instead of
    "all cut at once") so per-user cutting throughput has real variance, and
    places the banding start/finish inside the same window.
    """
    order.created_at = timeline["created"]
    order.confirmed_at = timeline["created"]
    for h in order.history:
        if h.to_status in timeline:
            h.created_at = timeline[h.to_status]

    if "cutting" in timeline:
        order.assigned_at = timeline["cutting"]
    if OrderStatus.dispatched.value in timeline:
        order.dispatched_at = timeline[OrderStatus.dispatched.value]

    if has_banding and "cutting" in timeline:
        cutting_ts = timeline["cutting"]
        if "cut" in timeline:
            span = (timeline["cut"] - cutting_ts).total_seconds()
            started = cutting_ts + timedelta(seconds=random.uniform(0.2, 0.6) * span)
            finished = started + timedelta(hours=random.uniform(0.5, 3))
            if order.banding_started_at is not None:
                order.banding_started_at = started
            if order.banding_finished_at is not None:
                order.banding_finished_at = finished
        elif order.banding_started_at is not None:
            # Still cutting: banding only started, somewhere after it began.
            order.banding_started_at = min(
                cutting_ts + timedelta(hours=random.uniform(0.5, 3)),
                datetime.utcnow(),
            )

    if "cut" in timeline and "cutting" in timeline:
        span = (timeline["cut"] - timeline["cutting"]).total_seconds()
        for board in order.boards:
            for piece in board.pieces:
                if piece.cut_at is not None:
                    piece.cut_at = timeline["cutting"] + timedelta(
                        seconds=random.uniform(0, span)
                    )

    db.commit()


def slugify(name: str) -> str:
    """Lowercase ascii slug without accents: 'Sucúa' -> 'sucua', 'Macas' -> 'macas'."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
    return "".join(ch for ch in ascii_str.lower() if ch.isalnum())


def make_cutlist(material_key: str) -> list[dict]:
    """Demo cutlist with unique labels (distinct optimization hash)."""
    i = next(_seq)
    return [
        {
            "priority": 1,
            "height": 600,
            "width": 400,
            "quantity": 2,
            "material_key": material_key,
            "label": f"Puerta {i}",
        },
        {
            "priority": 0,
            "height": 350,
            "width": 300,
            "quantity": 3,
            "material_key": material_key,
            "label": f"Estante {i}",
        },
        {
            "priority": 0,
            "height": 700,
            "width": 250,
            "quantity": 2,
            "material_key": material_key,
            "label": f"Lateral {i}",
        },
    ]


def build_inputs(board: ProductModel) -> tuple[list[dict], list[dict]]:
    """Materials (1 catalog board) + unique cutlist for an entity."""
    key = "b1"
    materials = [{"key": key, "source": "catalog", "product_id": board.id}]
    return materials, make_cutlist(key)


def make_cutlist_with_banding(material_key: str, edge_band_id: int) -> list[dict]:
    """Cutlist with edge banding on the main pieces."""
    i = next(_seq)
    banding = {"product_id": edge_band_id, "sides": ["top", "bottom", "left", "right"]}
    return [
        {
            "priority": 1,
            "height": 600,
            "width": 400,
            "quantity": 2,
            "material_key": material_key,
            "label": f"Puerta {i}",
            "edge_banding": banding,
        },
        {
            "priority": 0,
            "height": 350,
            "width": 300,
            "quantity": 3,
            "material_key": material_key,
            "label": f"Estante {i}",
            "edge_banding": {"product_id": edge_band_id, "sides": ["top", "bottom"]},
        },
        {
            "priority": 0,
            "height": 700,
            "width": 250,
            "quantity": 2,
            "material_key": material_key,
            "label": f"Lateral {i}",
        },
    ]


def build_inputs_with_banding(
    board: ProductModel, edge_band: ProductModel
) -> tuple[list[dict], list[dict]]:
    """Materials + cutlist with edge banding on some pieces."""
    key = "b1"
    materials = [{"key": key, "source": "catalog", "product_id": board.id}]
    return materials, make_cutlist_with_banding(key, edge_band.id)


# --------------------------------------------------------------------------- #
# Foundations (idempotent)                                                     #
# --------------------------------------------------------------------------- #


def ensure_branches(db) -> list[BranchModel]:
    """Creates/gets the branches from ``COMPANY_BRANCHES`` in the environment."""
    branches = []
    for entry in config.COMPANY_BRANCHES:
        name = entry["name"]
        code = slugify(name).upper()[:32]
        branch = db.query(BranchModel).filter(BranchModel.code == code).first()
        if branch is None:
            branch = BranchModel(
                code=code,
                name=name,
                address=entry.get("address"),
                is_active=True,
            )
            db.add(branch)
            db.flush()
            print(f"  + Branch {code} ({name})")
        else:
            print(f"  = Branch {code} ({name}) already existed")
        branches.append(branch)
    return branches


def ensure_user(db, email, full_name, role, branch_id) -> UserModel:
    """Creates/gets a user by email."""
    user = db.query(UserModel).filter(UserModel.email == email).first()
    if user is None:
        user = UserModel(
            email=email,
            full_name=full_name,
            hashed_password=hash_password(SEED_PASSWORD),
            role=role,
            is_active=True,
            branch_id=branch_id,
        )
        db.add(user)
        db.flush()
        print(f"  + User {email} ({role})")
    else:
        print(f"  = User {email} already existed")
    return user


def ensure_users(db, branches) -> tuple[UserModel, dict[int, dict[str, UserModel]]]:
    """Global admin + seller, operator and bander per branch. Returns (admin, staff per branch)."""
    admin = ensure_user(
        db, "admin@empresa.com", "Administrador General", UserRole.ADMIN.value, None
    )
    staff: dict[int, dict[str, UserModel]] = {}
    for branch in branches:
        slug = slugify(branch.name)
        seller = ensure_user(
            db,
            f"vendedor{slug}@empresa.com",
            f"Vendedor {branch.name}",
            UserRole.SELLER.value,
            branch.id,
        )
        operator = ensure_user(
            db,
            f"operador{slug}@empresa.com",
            f"Operador {branch.name}",
            UserRole.OPERATOR.value,
            branch.id,
        )
        bander = ensure_user(
            db,
            f"canteador{slug}@empresa.com",
            f"Canteador {branch.name}",
            UserRole.BANDER.value,
            branch.id,
        )
        staff[branch.id] = {
            "seller": seller,
            "operator": operator,
            "bander": bander,
        }
    return admin, staff


def ensure_clients(db) -> list[ClientModel]:
    """Creates/gets the 5 demo clients (all with a phone number)."""
    clients = []
    for identifier, first, last, phone, email in CLIENTS:
        client = (
            db.query(ClientModel).filter(ClientModel.identifier == identifier).first()
        )
        if client is None:
            client = ClientModel(
                identifier=identifier,
                first_name=first,
                last_name=last,
                phone=phone,
                email=email,
                source=SEED_SOURCE,
            )
            db.add(client)
            db.flush()
            print(f"  + Client {identifier} ({first} {last})")
        else:
            print(f"  = Client {identifier} already existed")
        clients.append(client)
    return clients


def ensure_boards(db) -> list[ProductModel]:
    """Reuses up to 3 boards from the catalog; if none, creates the demo boards."""
    existing = (
        db.query(ProductModel)
        .filter(ProductModel.type == ProductType.BOARD.value)
        .order_by(ProductModel.id)
        .limit(3)
        .all()
    )
    if existing:
        # Backfill the coordination family on reused DEMO boards (matched by name) that
        # predate the feature, so the optimizer can infer tapacantos in the demo. Real
        # catalog boards (names not in DEMO_BOARDS) already carry their own family.
        fam_by_name = {name: family for _c, name, *_rest, family in DEMO_BOARDS}
        for board in existing:
            fam = fam_by_name.get(board.name)
            if fam and not (board.attributes or {}).get("family"):
                board.attributes = {**(board.attributes or {}), "family": fam}
        db.flush()
        print(f"  = Using {len(existing)} existing board(s) from the catalog")
        return existing

    boards = []
    for code, name, height, width, thickness, price, family in DEMO_BOARDS:
        board = ProductModel(
            type=ProductType.BOARD.value,
            code=code,
            name=name,
            description=name,
            price=price,
            is_active=True,
            attributes={
                "height": height,
                "width": width,
                "thickness": thickness,
                "grainDirection": None,
                "family": family,
            },
        )
        db.add(board)
        boards.append(board)
        print(f"  + Board {code} ({name})")
    db.flush()
    return boards


def ensure_edge_bandings(db) -> list[ProductModel]:
    """Upserts the demo edge bandings, matched by name (unique + stable).

    Keying on the name (not the code) keeps this idempotent even after the demo
    codes change: a pre-existing row is updated in place (new code + attributes,
    including the coordination ``family``) instead of inserting a duplicate that
    would violate ``uq_products_name``.
    """
    edge_bands = []
    for code, name, thickness, width, band_type, price, family in DEMO_EDGE_BANDINGS:
        attributes = {
            "thickness": thickness,
            "width": width,
            "bandType": band_type,
            "color": None,
            "length": None,
            "family": family,
        }
        band = db.query(ProductModel).filter(ProductModel.name == name).first()
        if band is None:
            band = ProductModel(
                type=ProductType.EDGE_BANDING.value,
                code=code,
                name=name,
                description=name,
                price=price,
                is_active=True,
                attributes=attributes,
            )
            db.add(band)
            print(f"  + Edge banding {code} ({name})")
        else:
            band.code = code
            band.price = price
            band.attributes = attributes
            print(f"  = Edge banding {name} synced ({code})")
        edge_bands.append(band)
    db.flush()
    return edge_bands


# --------------------------------------------------------------------------- #
# Transactional data (pre-orders and orders)                                   #
# --------------------------------------------------------------------------- #


def seed_preorder(db, status, branch, client, seller, board):
    """Creates a pre-order and drives it to the target ``status`` via the real flow."""
    pre_svc = PreOrderService(db)
    review_svc = PreOrderReviewService(db)
    actor = staff_actor(seller)

    materials, requirements = build_inputs(board)
    preorder = pre_svc.create(
        PreOrderCreate(
            materials=materials,
            requirements=requirements,
            client_id=client.id,
            branch_id=branch.id,
            source=SEED_SOURCE,
            notes=f"Pre-orden demo en estado {status.value}",
        ),
        actor=actor,
        branch_scope=None,  # admin route: uses branch_id from the body
    )

    if status == PreOrderStatus.draft:
        pass

    elif status == PreOrderStatus.sent:
        review_svc.generate(preorder.id, actor=actor)

    elif status == PreOrderStatus.changes_requested:
        _, token = review_svc.generate(preorder.id, actor=actor)
        review_svc.request_changes(
            token, note="¿Pueden ajustar la medida de las puertas?"
        )

    elif status == PreOrderStatus.confirmed:
        _, token = review_svc.generate(preorder.id, actor=actor)
        review_svc.confirm(token, note="Aprobado por el cliente")

    elif status == PreOrderStatus.rejected:
        _, token = review_svc.generate(preorder.id, actor=actor)
        review_svc.reject(token, note="El cliente prefirió otra cotización")

    elif status == PreOrderStatus.expired:
        # Expire its validity and let the lazy sweep mark it 'expired'.
        preorder.expires_at = datetime.utcnow() - timedelta(days=1)
        db.commit()
        pre_svc.get_or_404(preorder.id)

    elif status == PreOrderStatus.cancelled:
        # There's no cancellation endpoint today; the transition is recorded by hand
        # to have the status represented in the demo.
        pre_svc._record_transition(
            preorder,
            preorder.status,
            PreOrderStatus.cancelled,
            actor,
            note="Cancelada por el vendedor",
        )
        preorder.status = PreOrderStatus.cancelled.value
        db.commit()

    db.refresh(preorder)
    return preorder


def _mark_all_pieces_cut(svc: OrderService, order_id: int, actor):
    """Marks all placed pieces as cut (requires 'cutting' status)."""
    order = svc.get_or_404(order_id)
    for board in order.boards:
        for piece in board.pieces:
            svc.mark_piece_cut(order_id, piece.id, cut=True, actor=actor)


def drive_order_to(svc, order, target, seller_actor, operator_actor, bander_actor=None):
    """Advances the order through the state machine up to ``target``.

    If ``bander_actor`` is given, also advances the banding track according to
    the target status: ``cutting`` → in_progress; ``cut``/``completed`` → done.
    """
    has_banding = bander_actor is not None

    if target == OrderStatus.confirmed:
        return
    if target == OrderStatus.cancelled:
        svc.transition(
            order.id, OrderStatus.cancelled, actor=seller_actor, note="Cancelada"
        )
        return

    # confirmed → queued requires a payment method (at least one amount > 0).
    svc.transition(
        order.id,
        OrderStatus.queued,
        actor=seller_actor,
        note="Enviada a la cola de producción",
        payment=OrderPaymentInput(cash_amount=500.00),
    )
    if target == OrderStatus.queued:
        return

    # The operator self-assigns when taking the cut.
    svc.transition(
        order.id,
        OrderStatus.cutting,
        actor=operator_actor,
        note="Operador toma el corte",
    )
    if target == OrderStatus.cutting:
        if has_banding:
            svc.transition_banding(
                order.id, BandingStatus.in_progress, actor=bander_actor
            )
        return

    # To close the cut: mark all pieces and finish banding first.
    _mark_all_pieces_cut(svc, order.id, operator_actor)
    if has_banding:
        svc.transition_banding(order.id, BandingStatus.in_progress, actor=bander_actor)
        svc.transition_banding(order.id, BandingStatus.done, actor=bander_actor)
    svc.transition(
        order.id, OrderStatus.cut, actor=operator_actor, note="Corte finalizado"
    )
    if target == OrderStatus.cut:
        return

    svc.transition(
        order.id, OrderStatus.completed, actor=seller_actor, note="Pedido entregado"
    )
    if target == OrderStatus.completed:
        return

    # completed → despachado: any role can dispatch.
    svc.transition(
        order.id,
        OrderStatus.dispatched,
        actor=seller_actor,
        note="Mercadería entregada",
    )


def seed_order(
    db, status, branch, client, seller, operator, board, bander=None, edge_band=None
):
    """Creates an order and drives it to the target ``status`` via the state machine.

    If ``bander`` and ``edge_band`` are provided, the order includes edge banding
    and the banding track is advanced according to the target status.
    """
    svc = OrderService(db)
    seller_actor = staff_actor(seller)
    operator_actor = staff_actor(operator)
    bander_actor = staff_actor(bander) if bander and edge_band else None

    if bander_actor:
        materials, requirements = build_inputs_with_banding(board, edge_band)
        notes = f"Orden demo con tapacantos en estado {status.value}"
    else:
        materials, requirements = build_inputs(board)
        notes = f"Orden demo en estado {status.value}"

    order = svc.create(
        OrderCreate(
            materials=materials,
            requirements=requirements,
            client_id=client.id,
            branch_id=branch.id,
            source=SEED_SOURCE,
            notes=notes,
        ),
        actor=seller_actor,
    )
    drive_order_to(svc, order, status, seller_actor, operator_actor, bander_actor)
    db.refresh(order)
    return order


def seed_transactional(db, branches, clients, staff, boards, edge_bands):
    """One pre-order and one order in each status, per branch.

    Orders in workshop statuses (cutting/cut/completed) are created with edge
    banding to demonstrate the banding track with the branch's bander.
    """
    # Statuses that must include edge banding to demonstrate the banding flow.
    BANDING_STATUSES = {
        OrderStatus.cutting,
        OrderStatus.cut,
        OrderStatus.completed,
        OrderStatus.dispatched,
    }

    for branch in branches:
        seller = staff[branch.id]["seller"]
        operator = staff[branch.id]["operator"]
        bander = staff[branch.id]["bander"]
        print(f"\n  Branch {branch.name}:")

        for i, status in enumerate(PREORDER_STATUSES):
            client = clients[i % len(clients)]
            board = boards[i % len(boards)]
            preorder = seed_preorder(db, status, branch, client, seller, board)
            print(f"    + Pre-order {preorder.code} [{preorder.status}]")

        for i, status in enumerate(ORDER_STATUSES):
            client = clients[(i + 2) % len(clients)]
            board = boards[i % len(boards)]
            use_banding = status in BANDING_STATUSES and edge_bands
            edge_band = edge_bands[i % len(edge_bands)] if use_banding else None
            order = seed_order(
                db,
                status,
                branch,
                client,
                seller,
                operator,
                board,
                bander=bander if use_banding else None,
                edge_band=edge_band,
            )
            banding_note = f" (canteado: {order.banding_status})" if use_banding else ""
            print(f"    + Order {order.code} [{order.status}]{banding_note}")


def seed_analytics_orders(db, branches, clients, staff, boards, edge_bands):
    """Extra backdated orders per status, per branch: volume/spread for analytics.

    Reuses ``seed_order`` (the real state machine, so business fields like
    ``assigned_to_id``/billing lines stay correct) and then rewrites its
    timestamps via ``build_order_timeline``/``backdate_order`` so
    ``/analytics/bottlenecks`` (stage durations) and ``/analytics/users``
    (per-operator cutting throughput) show realistic variance instead of
    everything happening in the same instant.
    """
    BANDING_STATUSES = {
        OrderStatus.cutting,
        OrderStatus.cut,
        OrderStatus.completed,
        OrderStatus.dispatched,
    }
    total = 0
    for branch in branches:
        seller = staff[branch.id]["seller"]
        operator = staff[branch.id]["operator"]
        bander = staff[branch.id]["bander"]

        for status in ORDER_STATUSES:
            for _ in range(ORDERS_PER_STATUS_ANALYTICS):
                client = random.choice(clients)
                board = random.choice(boards)
                use_banding = bool(
                    status in BANDING_STATUSES and edge_bands and random.random() < 0.6
                )
                edge_band = random.choice(edge_bands) if use_banding else None
                order = seed_order(
                    db,
                    status,
                    branch,
                    client,
                    seller,
                    operator,
                    board,
                    bander=bander if use_banding else None,
                    edge_band=edge_band,
                )
                timeline = build_order_timeline(status)
                backdate_order(db, order, timeline, has_banding=use_banding)
                total += 1
        print(
            f"    + {ORDERS_PER_STATUS_ANALYTICS * len(ORDER_STATUSES)} backdated orders in {branch.name}"
        )
    print(f"  Analytics history: {total} extra orders generated.")


def seed_login_events(db, users):
    """Synthetic login history (last ``ATTENDANCE_DAYS`` business days) per user.

    Approximates a daily clock-in around the user's role hour (jittered), with
    occasional absences and occasional second logins later in the day, so
    ``/analytics/attendance`` has something to chart.
    """
    now = datetime.utcnow()
    created = 0
    for user in users:
        base_hour = ROLE_BASE_HOUR.get(user.role, 8.0)
        for days_ago in range(ATTENDANCE_DAYS, -1, -1):
            day = (now - timedelta(days=days_ago)).date()
            if day.weekday() >= 5:  # skip weekends
                continue
            if random.random() < 0.06:  # occasional absence
                continue
            hour = max(0.0, base_hour + random.uniform(-0.4, 0.6))
            local_login = datetime.combine(
                day, datetime.min.time(), tzinfo=LOCAL_TZ
            ) + timedelta(hours=hour)
            first_login = local_login.astimezone(timezone.utc).replace(tzinfo=None)
            db.add(UserLoginEventModel(user_id=user.id, created_at=first_login))
            created += 1
            if random.random() < 0.3:  # occasional second login later in the day
                second = first_login + timedelta(hours=random.uniform(2, 6))
                db.add(UserLoginEventModel(user_id=user.id, created_at=second))
                created += 1
    db.commit()
    print(f"  + {created} login events for {len(users)} user(s)")


def reset_demo(db):
    """Deletes the demo pre-orders/orders (``source='seed'``) and login events.

    Pre-orders first (they reference the order via ``order_id``). Login events
    are matched by the seeded users' well-known email patterns.
    """
    pres = db.query(PreOrderModel).filter(PreOrderModel.source == SEED_SOURCE).all()
    for p in pres:
        db.delete(p)
    db.flush()
    orders = db.query(OrderModel).filter(OrderModel.source == SEED_SOURCE).all()
    for o in orders:
        db.delete(o)

    demo_user_ids = [
        u.id
        for u in db.query(UserModel)
        .filter(
            or_(
                UserModel.email == "admin@empresa.com",
                UserModel.email.like("vendedor%@empresa.com"),
                UserModel.email.like("operador%@empresa.com"),
                UserModel.email.like("canteador%@empresa.com"),
            )
        )
        .all()
    ]
    deleted_logins = 0
    if demo_user_ids:
        deleted_logins = (
            db.query(UserLoginEventModel)
            .filter(UserLoginEventModel.user_id.in_(demo_user_ids))
            .delete(synchronize_session=False)
        )

    db.commit()
    print(
        f"Reset: deleted {len(pres)} demo pre-orders, {len(orders)} demo orders "
        f"and {deleted_logins} login events.\n"
    )


def main():
    parser = argparse.ArgumentParser(description="Cutter demo seed.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete previous demo pre-orders/orders before regenerating them.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.reset:
            reset_demo(db)

        print("Branches:")
        branches = ensure_branches(db)
        print("Users:")
        admin, staff = ensure_users(db, branches)
        print("Clients:")
        clients = ensure_clients(db)
        print("Boards and edge bandings:")
        boards = ensure_boards(db)
        edge_bands = ensure_edge_bandings(db)
        db.commit()

        existing_demo = (
            db.query(PreOrderModel).filter(PreOrderModel.source == SEED_SOURCE).count()
        )
        if existing_demo and not args.reset:
            print(
                f"\n{existing_demo} demo pre-orders already exist; skipping transactional "
                "generation. Use --reset to regenerate them."
            )
        else:
            print("\nPre-orders and orders (one per status, per branch):")
            seed_transactional(db, branches, clients, staff, boards, edge_bands)
            db.commit()

            print(
                "\nAnalytics history (extra backdated orders for bottlenecks/productivity):"
            )
            seed_analytics_orders(db, branches, clients, staff, boards, edge_bands)
            db.commit()

            print("\nAttendance (login events):")
            all_users = [admin] + [
                u for role_map in staff.values() for u in role_map.values()
            ]
            seed_login_events(db, all_users)
            db.commit()

        print("\n✅ Seed complete.")
        print("   Users: admin@empresa.com + seller/operator/bander per branch")
        print(f"   Password for all users: {SEED_PASSWORD}")
    except Exception as e:
        db.rollback()
        print(f"\n❌ Error: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
