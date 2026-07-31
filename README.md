# Cutter API

Cutter is a REST API, built with Python and FastAPI, that powers the full
order-to-production workflow for a melamine board cutting business: it optimizes
2D guillotine cuts to minimize waste and cost, then carries a quote through
client review, confirmation, workshop production (cutting + edge banding) and
dispatch.

## Highlights

- **2D guillotine cutting optimizer** — a pure, framework-free domain layer
  (`src/cutting/`) that packs pieces onto boards with a selectable heuristic
  (max efficiency, or one that concentrates waste into a single reusable
  offcut), respecting kerf and trim margins.
- **Material-source agnostic** — the optimizer accepts stock from the product
  catalog, company/client offcuts, or ad-hoc manual entries through a single
  resolver, so `cutting/` only ever deals with geometry.
- **Deterministic, cached results** — identical optimization requests hash to
  the same Redis-cached result; nothing is persisted until a client commits to
  an order.
- **Quote → order lifecycle** — mutable pre-orders (quotes) become immutable
  order snapshots with frozen prices; orders carry a production status machine
  (`confirmed → queued → cutting → cut → completed → dispatched`) plus an
  independent edge-banding track for the `canteador` role.
- **Commercial & production documents** — PDF proforma/order documents,
  production sheets and dispatch sheets, all rendered from the same snapshot
  and including a cutting diagram.
- **JWT auth + RBAC** — short-lived access tokens, rotating refresh tokens, and
  a role-based permission matrix (`administrador`, `vendedor`, `operador`,
  `canteador`) enforced centrally.
- **Multi-branch isolation** — orders, pre-orders and drafts are scoped to a
  branch; clients and the product catalog remain global.
- **Analytics** — read-only reporting on revenue, throughput, bottlenecks, user
  productivity and attendance.

## Architecture at a glance

Vertical-slice modules (`src/modules/<name>/{model,schemas,service,router}.py`)
sit on top of a pure cutting-optimization domain and a small shared
infrastructure layer. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for
the full breakdown.

```
src/cutting/     pure Python — the optimization algorithm, no framework imports
src/shared/      infrastructure — DB, generic CRUD, error handling, auth primitives, config
src/modules/*/   feature slices — depend on shared/ and cutting/, never on each other in a cycle
main.py          wires routers, middleware and error handlers
```

## Getting started

### Requirements

- Docker and Docker Compose (recommended), or Python 3.11+ with a local
  PostgreSQL 16 and Redis instance.

### Run with Docker

```bash
cp .env.example .env
docker compose up -d --build
```

Open the interactive API docs at `http://localhost:8000/docs`.

### Run locally

```bash
make run-local        # requires PostgreSQL reachable at localhost:5433 (see docs/DATABASE_SETUP.md)
```

### Useful commands

```bash
make tests             # full test suite (Docker) against a dedicated cutter_test_db
make lint-check-local   # ruff check + format check
make migrations m="description"   # generate an Alembic migration
make upgrade                      # apply pending migrations
```

Run `make help` for the full list of targets (build, dev, seeding scripts,
migrations, etc.).

## API conventions

- All request/response bodies use **camelCase** (input also accepts
  snake_case); internally the codebase stays in snake_case
  (`src/shared/schemas.py::CamelModel`).
- Successful responses wrap the payload in `{"data": ..., "meta": {...}}`;
  paginated lists add `meta.pagination`. Errors share a uniform envelope:
  `{"errors": [{"code", "message", "field?"}], "meta": {...}}`
  (`src/shared/responses.py`, `src/shared/exceptions.py`).
- Authentication is a JWT bearer access token plus an opaque, rotating refresh
  token; authorization is role-based. See [`docs/AUTH.md`](docs/AUTH.md).
- PDF documents (proforma, order, production sheet, dispatch sheet) and the
  public client-review endpoints are the only routes exempt from the standard
  envelope.

### Example: run an optimization

```bash
curl -X POST "http://localhost:8000/api/v1/optimize" \
  -H "Authorization: Bearer <accessToken>" \
  -H "Content-Type: application/json" \
  -d '{
    "materials": [
      {
        "key": "MEL18", "source": "manual",
        "height": 2440, "width": 1220, "thickness": 18, "costPerUnit": 45.50
      }
    ],
    "requirements": [
      {
        "materialKey": "MEL18", "priority": 1,
        "height": 400, "width": 600, "quantity": 2, "label": "Top shelf"
      }
    ]
  }'
```

The response includes the cutting layout per board, board count, edge-banding
length and total waste; nothing is persisted — the result is cached in Redis
by a deterministic hash of the request.

## Documentation

| Doc | Covers |
|-----|--------|
| [Architecture](docs/ARCHITECTURE.md) | Module layout, dependency rules, shared building blocks, DB/migrations |
| [Authentication & RBAC](docs/AUTH.md) | Login/refresh flow, roles, the per-endpoint permission matrix |
| [Database setup](docs/DATABASE_SETUP.md) | PostgreSQL configuration and the Alembic migration workflow |
| [Testing](docs/TESTING.md) | Unit vs. integration layers, commands, how to write each kind |
| [Multi-branch](docs/MULTI_BRANCH.md) | Branch isolation rules for orders, pre-orders and drafts |
| [Cutting diagram rendering](docs/CUTTING_DIAGRAM.md) | The Pillow-based renderer used inside the PDF documents |

## Notes

- If Redis is unavailable, the service keeps operating, just without caching.
- The packing engine minimizes **material cost**, not just piece placement: a
  beam search over board partitions, local-search repair, and an OR-Tools CP-SAT
  solver that decides the tight last board exactly. It often proves optimality
  (matching the area lower bound) and stops there; otherwise it returns the best
  solution found within a deterministic budget.
- `ortools` is optional at runtime — without it the engine falls back to its
  heuristic search, at some cost in board count.
