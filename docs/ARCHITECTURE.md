# Architecture

## Overview

Cutter is a FastAPI service organized as **vertical slices**: each business
resource owns its router, service, schemas and ORM model in a single folder.
This keeps a resource's code cohesive, avoids empty "use case" layers and
per-entity repositories, and keeps the cutting algorithm isolated from any
framework concern.

Design goal: **pragmatic and easy to read**. SOLID where it earns its keep,
without ceremony — a generic CRUD base and a single error-handling pipeline
replace most of the boilerplate a layered architecture would otherwise need.

## Project layout

```
src/
├── shared/                    Cross-cutting infrastructure
│   ├── config.py               Single Config instance (env-driven: KERF, trims, CORS, DB...)
│   ├── database.py             Declarative Base, engine, SessionLocal, get_db
│   ├── schemas.py              CamelModel — camelCase JSON contract
│   ├── exceptions.py           AppError hierarchy with HTTP status_code
│   ├── errors.py               register_exception_handlers(app)
│   ├── crud.py                 CRUDService[Model, Create, Update] generic base
│   ├── security.py             bcrypt hashing, JWT issue/verify, opaque token generation
│   ├── responses.py            DataResponse/PaginatedResponse/ErrorResponse envelopes
│   ├── pagination.py           PageParams (limit/offset query params)
│   ├── audit.py                created_by/updated_by actor resolution
│   ├── branch_scope.py         Branch-isolation query helpers
│   ├── context.py              Per-request context (request ID, current user)
│   ├── middleware.py           CurrentUserMiddleware, RequestIDMiddleware
│   ├── cache.py                Redis client wrapper
│   └── mixins.py               TimestampMixin, AuditMixin for ORM models
├── modules/                    One slice per resource: {model,schemas,service,router}.py
│   ├── users/                   Staff users, auth (JWT + refresh), RBAC source of truth
│   ├── branches/                Branch CRUD (multi-branch isolation)
│   ├── clients/                 Client management (CRUD)
│   ├── products/                Unified product catalog (board / edge_banding / hardware)
│   ├── optimizations/           Cutting optimizer orchestration, PDF documents, diagrams
│   ├── optimization_drafts/     Saved, named, mutable optimizer inputs (work in progress)
│   ├── preorders/                Mutable quotes + client review links
│   ├── orders/                  Aggregate root: immutable snapshot, status machine, billing
│   ├── settings/                Editable runtime configuration (cutting, pricing, company, preorders)
│   ├── analytics/               Read-only reporting
│   └── system/                  Health / readiness endpoints
├── cutting/                    Pure domain — NO framework imports
│   ├── models.py                Rectangle, Piece, PlacedPiece, Material, BinSpec, CuttingLayout
│   ├── enums.py                 SplitRule, PackingStrategy
│   ├── parameters.py            CuttingParameters (dataclass)
│   ├── packer.py                GuillotineOptimizer — packs ONE bin
│   ├── constructors.py          Candidate bin fills (greedy portfolio, strip patterns)
│   ├── exact.py                 CP-SAT endgame: exact 2-stage fill of one bin (OR-Tools)
│   └── search.py                optimize_bins — board count as an explicit cost objective
main.py                         Creates the app, registers routers + middleware + handlers
alembic/                        Migrations (env.py imports Base and every module's model)
```

## Dependency rules

No cycles, enforced by convention:

- **`cutting/`** imports nothing from `shared/` or `modules/`. It stays pure
  and unit-testable in isolation.
- **`shared/`** depends on nothing in `modules/`.
- **`modules/*`** depend on `shared/` and `cutting/`.
- Cross-module dependencies flow one way only — e.g. `optimizations` reads
  from `products`/`clients` (catalog lookups, client metadata) and `orders`
  reads from `preorders`/`optimizations`, never the reverse. `users` is the
  foundational auth module: every other module imports its `dependencies.py`
  for permission checks, and `users` imports nothing from them.

## Feature modules

- **`users`** — staff accounts, JWT auth (short-lived access token + rotating
  opaque refresh token), and the RBAC matrix (`permissions.py::RESOURCE_ROLES`)
  that every other module's router depends on. Roles: `administrador`,
  `vendedor` (both global), `operador`, `canteador` (both branch-scoped
  workshop roles). See [`AUTH.md`](AUTH.md).
- **`branches`** — CRUD for physical locations. Orders, pre-orders and
  optimization drafts are scoped to a branch; clients and the product catalog
  stay global. See [`MULTI_BRANCH.md`](MULTI_BRANCH.md).
- **`clients`** — client CRUD, search by phone/identifier.
- **`products`** — a single `products` table backs every catalog item
  (`type`: `board` / `edge_banding` / `hardware`), with type-specific fields
  validated by a Pydantic discriminated union over a JSON `attributes` column.
  Add a new product type by registering a schema under `products/types/` —
  no migration needed. Only `board`-typed products feed the optimizer.
  `GET /products/{board_id}/edge-bandings` matches the edge bandings designed
  for a given board by the explicit `family` attribute they share, then applies
  a thickness→width rule (over active products only). It is **advisory for the
  UI**: nothing downstream requires a piece's edge banding to be coordinated,
  so a contrasting one can be quoted deliberately. Board and edge-banding
  attributes also carry an optional `subtype` (material subtype — MDP/MDF/
  Plywood/Pine/... for boards, Wood Grain/Solid/Gloss/... for edge bandings,
  each a closed enum with canonical English values; the external catalog sync
  feeds it the vendor's Spanish `TIPO`/`GRUPO` text, normalized via input
  aliases). Edge bandings additionally carry an `alias` (short, ≤20 chars)
  used only in the printed workshop notation (`2L1C CS CSH`) — independent of
  `family`, which remains the coordination key and is never printed. Both come
  from a single free-text column in the vendor's inventory (`marticulo.obs`),
  written **`FAMILIA`** or **`FAMILIA - ALIAS`**: the column *is* the family,
  and the short code is an optional suffix behind `" - "`. The family leads
  because it is the half that always applies — a board has no alias to write,
  so writing just the family has to be valid input in both categories. Both
  categories parse identically (the board simply drops the alias), which is
  what makes the likeliest data-entry slip harmless: the same text pasted on a
  board and on its tapacanto still coordinates them. Two details guard the
  edges — the split takes the **last** separator, so a hyphen inside a design's
  own name stays with the family, and the right-hand side only counts as an
  alias if it looks like a code (no spaces, ≤20 chars); otherwise the whole
  column is the family. The
  catalog is also synced in bulk **straight from the external inventory
  system's MySQL** (`POST /products/sync`, no body — it replaced an upload of
  that system's CSV export, so nobody has to export and re-upload a file).
  `external_catalog.py` owns the vendor schema (`marticulo`: the whole
  `TABLEROS` category plus `TAPACANTOS` narrowed to its homonymous `tip`,
  since that category holds a second `tip` that isn't edge banding) and hands
  `catalog_sync.py` plain text rows; everything downstream is source-agnostic.
  Upsert is by a namespaced `external_code` (`"{CATEGORIA}:{CODIGO}"`, where
  CODIGO is the vendor's `cin` column — **not** its `cod` primary key, which is
  an unrelated autoincrement; keying on `cod` recreates the catalog under wrong
  codes), and
  products the read no longer brings are reconciled (deleted if no order used
  them, deactivated otherwise). Three safeguards specific to reading a live
  database rather than an uploaded file: (1) a row whose data doesn't parse is
  **skipped and reported** (`ProductSyncResult.issues`, carrying the vendor's
  code and article name) instead of aborting — the vendor's table permanently
  contains articles with no usable dimensions, and they can't be "fixed and
  re-uploaded" before every run; skipped rows are also excluded from the
  reconciliation pass, since *we couldn't read it* must not be confused with
  *the vendor removed it*; (2) retirement is **explicit** (`est`/`FecEli`), and
  retired rows never reach validation; (3) a read with zero active rows aborts
  rather than letting reconciliation empty the catalog. What still aborts with
  zero writes is a clash with the catalog itself — a name or code owned by a
  hand-created product — because that's a problem at the destination.
  `?dryRun=true` runs the whole pass and rolls back. Separately from `issues`
  (rows that were skipped), the result carries `warnings` — rows that **were**
  imported but whose design data can't do its job: an edge banding with no
  family or no alias, or a family declared on only one of the two categories.
  None of it blocks the sync; it is reported because board↔edge-banding
  coordination is an equality match that fails **silently** (a board left on
  `Cashmere` while its banding moved to `CSH` just returns an empty picker),
  and the dry run is where an operator would want to see it. A board with no
  family is deliberately *not* warned — plywood, OSB and MDF fondo have no
  coordinated banding at all.
- **`optimizations`** — orchestrates the pure `cutting/` domain. `POST
  /optimize` is **material-source agnostic**: it takes a `materials` stock
  list (catalog boards, company/client offcuts, or manual entries, unified by
  a `MaterialInput` discriminated union) and `requirements` that reference a
  material by `materialKey`, never a catalog ID directly — `cutting/` only
  ever sees geometry. A pluggable `strategy` (`default` for maximum
  efficiency, `longOffcuts` to concentrate waste into one reusable strip)
  affects both the geometry and the result's cache key. Results are
  deterministic and cached in Redis by a hash of the canonical request — nothing
  is persisted to the database here. This module also owns the rendering
  pipeline shared by every PDF document (proforma, order, production sheet,
  dispatch sheet): `proforma.py` (document layout via ReportLab) and
  `visualization.py` (the cutting diagram via Pillow — see
  [`CUTTING_DIAGRAM.md`](CUTTING_DIAGRAM.md)).
- **`optimization_drafts`** — lets a seller save a named, editable optimizer
  input (materials + requirements + strategy) before turning it into a
  pre-order.
- **`preorders`** — mutable quotes. A pre-order re-optimizes from its stored
  `materials`/`requirements`/`strategy` on every read, so prices and layout
  always reflect current catalog/settings. Supports a client-review flow:
  `POST /preorders/{id}/review-link` issues a single-use, sha256-hashed
  token; the public, token-gated endpoints in `public_router.py` let the
  client confirm or reject without a staff session.
- **`orders`** — the durable aggregate root: an **immutable snapshot** of an
  optimization plus frozen prices, billed by **product**
  (`order_lines → product_id`, nullable for non-catalog materials) rather
  than by raw cut piece. Order creation also materializes the snapshot into
  `order_boards` / `order_placed_pieces` (one row per physical board/piece)
  for workshop tracking. Two independent status tracks:
  - **Cutting**: `confirmed → queued → cutting → cut → completed →
    dispatched` (plus a `confirmed → cancelled` escape and an admin
    `cutting → queued` rollback). The `confirmed → queued` transition
    requires recording a payment method (cash/credit, informational only).
  - **Banding** (edge banding, `banding_status`): `pending → in_progress →
    done`, advanced independently by the `canteador` role while cutting is
    still in progress. An order with edge banding can't reach `completed`
    until banding is `done`.

  Orders render their own commercial document, production sheet and dispatch
  sheet from the frozen snapshot.
- **`settings`** — a singleton row holding runtime-editable configuration
  (cutting parameters, price tiers, company info, pre-order policy) that is
  only seeded from environment variables on first read; `PATCH` endpoints are
  the source of truth afterwards.
- **`analytics`** — read-only reporting: revenue summaries, time series,
  status/branch breakdowns, bottleneck detection, user productivity and
  attendance (derived from login events).
- **`system`** — health and readiness checks.

## Shared building blocks

### `shared/crud.py` — generic CRUD base

`CRUDService[ModelT, CreateT, UpdateT]` centralizes `get` / `get_or_404` /
`list` / `create` / `update` / `delete` plus translation of `IntegrityError`
into a domain exception. A service only declares its `model` and, optionally,
`conflict_messages` (a substring of the DB constraint name → a human-readable
message) plus whatever resource-specific methods it needs (`search`,
`get_by_phone`, ...).

### `shared/exceptions.py` + `shared/errors.py` — centralized errors

A small hierarchy carrying its own `status_code`: `EntityNotFoundError`
(404), `ConflictError` (409), `AuthenticationError` (401),
`AuthorizationError` (403), `BusinessRuleError`/`ValidationError` (422), base
`AppError` (400). Services and the domain raise these **without knowing
about FastAPI**; a single handler registered via
`register_exception_handlers(app)` translates them into the uniform
`{"errors": [...], "meta": {...}}` envelope. This removes repeated
`if not x: raise HTTPException(404)` checks from every route.

### Dependency injection + thin routes

Each module exposes a provider (e.g. `client_service(db = Depends(get_db))`).
Routes stay short and debuggable, with permission checks declared at the
router or route level:

```python
@router.get("/{client_id}", response_model=DataResponse[ClientResponse])
def get_client(client_id: int, svc: ClientService = Depends(client_service)):
    return ok(svc.get_or_404(client_id))
```

### `cutting/` — the optimizer domain

Pure dataclasses (`Piece`, `Material`, `BinSpec`, `CuttingLayout`, ...) and the
guillotine engine, layered: `packer.py` fills one bin, `constructors.py`
generates candidate fills of one bin, `exact.py` solves one bin exactly with
CP-SAT, and `search.py` (`optimize_bins`) chooses the cheapest *set* of bins —
board count is an explicit cost objective, not an emergent side effect, which is
what lets a half board be targeted on purpose.

`optimizations` orchestrates this domain: it resolves materials into `BinSpec`s
(including the half-board sibling), passes the `PackingStrategy` and the search
budgets, and raises `EntityNotFoundError` if a requirement references an unknown
catalog board instead of silently dropping it.

OR-Tools is the one heavyweight dependency here and it is **optional at
runtime**: if `ortools` can't be imported, `exact.py` returns `None` everywhere
and the engine falls back to its heuristic search. Results stay deterministic
either way — the optimizer payload is cached by a hash of its inputs, and that
hash includes the engine version, the search budgets and whether the solver is
available.

## API contract

Every schema extends `CamelModel`: the external contract is **camelCase**
(snake_case is also accepted on input); internally the codebase works in
snake_case. Responses are built directly from ORM models
(`from_attributes=True`) and wrapped in the envelopes defined in
`shared/responses.py`.

## Database & migrations

`shared/database.py` defines the common declarative `Base`; every ORM model
extends it. `alembic/env.py` points at `src.shared.database.Base` and imports
every module's `model.py` so `alembic revision --autogenerate` can see all
tables. The connection URL comes from `config.DATABASE_URL`. PostgreSQL is
the only supported engine — see [`DATABASE_SETUP.md`](DATABASE_SETUP.md).

## Running & verifying

- **App locally**: `make run-local` (`ENVIRONMENT=local`, requires PostgreSQL
  on `localhost:5433`).
- **Tests**: `make tests` (Docker, full suite + 80% coverage gate) or the
  unit-only fast loop described in [`TESTING.md`](TESTING.md). Coverage spans
  the optimizer domain, generic CRUD (including 409/404 paths), the
  optimization/order/pre-order flows, document rendering and the auth/RBAC
  layer.
- **Lint**: `make lint-check-local` (ruff check + format check).
- **Migrations**: `alembic upgrade head`; `alembic revision --autogenerate`
  should produce no diff when models match the live schema.
