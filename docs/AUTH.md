# Authentication & Authorization (RBAC)

Auth contract for the Cutter API, consumed by the Maderable web dashboard
(React). Every endpoint except the public ones requires an **access token**
(JWT) in the `Authorization: Bearer <accessToken>` header. Authorization is
**role-based**, resolved against a single permission matrix.

## Roles

| Role (DB value) | Description |
|------------------|-------------|
| `administrador`  | Full access (management, settings, analytics). |
| `vendedor`       | Sales: product catalog (read), additional-services catalog (full CRUD), clients, optimizer, pre-orders, orders. |
| `operador`       | Workshop (cutting): reads orders, drives the cutting plan, marks pieces cut. |
| `canteador`      | Workshop (edge banding): the shared shop-floor board, the start/finish banding actions, and completing orders; does **not** see order detail. |
| `proyectista`    | Design: the optimizer, the catalog (read) and **their own** projects (`/projects`), which they hand to a seller to quote. No clients, pre-orders or orders. |

`operador` and `canteador` are workshop roles bound to a single branch;
`administrador` and `vendedor` are global (see
[`MULTI_BRANCH.md`](MULTI_BRANCH.md)). `proyectista` is neither: it needs a
base branch like a seller, but its projects are scoped by **owner**, not by
branch.

### Several roles per user

A user holds a **list** of roles (`roles`) and is granted the **union** of their
permissions, at every layer: the area matrix below, `TRANSITION_ROLES` and
`ACTIVITY_ROLES`. The case it exists for is a `canteador` learning to cut:
`["operador", "canteador"]` takes the order, marks pieces cut, and registers
the cutting, banding and additional work.

- **Only the workshop roles combine** (`operador` + `canteador`).
  `administrador`, `vendedor` and `proyectista` are exclusive; any other combination, or an
  empty list, is a `422` with `field: "roles"`. So a user with several roles is
  always a workshop user, bound to their branch.
- `roles` comes back in **canonical order** (`administrador`, `vendedor`,
  `operador`, `canteador`, `proyectista`), whatever order it was sent in.
- `?role=` filters (users listing, analytics) match a user **holding** that role.
- `roles` is **required** on `POST /users`. The single `role` of the first
  contract is gone (migration 016): no response carries it, and a body sending
  it is ignored like any unknown field — so a creation without `roles` is a
  `422`, never a silent default.

Login is by `email` (unique). Passwords are stored only as a bcrypt hash.

## Session flow

1. **Login** → `POST /api/v1/auth/login` with `{ email, password }`.
   Response: `{ accessToken, refreshToken, tokenType: "bearer", expiresIn, user }`
   (`user.roles` is the list of roles; the JWT's `roles` claim is informational,
   because every request re-reads the live roles from the database).
   - `accessToken`: short-lived JWT (`ACCESS_TOKEN_EXPIRE_MINUTES`, default 30 min).
   - `refreshToken`: long-lived opaque token (`REFRESH_TOKEN_EXPIRE_DAYS`, default 1
     day). Kept short as a backstop: the frontend already forces a real login once
     per calendar day, since attendance (`user_login_events`) is only recorded on
     `POST /auth/login`, never on `refresh` — see "Session flow" above.
   - `expiresIn`: access token lifetime in seconds.
2. **Call the API** → send `Authorization: Bearer <accessToken>` on every request.
3. **Refresh** → when the access token expires (or on a `401`), call
   `POST /api/v1/auth/refresh` with `{ refreshToken }`. Returns a **new pair**
   (same shape as login). The submitted refresh token **rotates**: it is
   invalidated and a new one is issued. Always persist the most recent
   `refreshToken`.
4. **Logout** → `POST /api/v1/auth/logout` with `{ refreshToken }` (revokes
   that refresh token; idempotent, `204`).

### Refresh token security (rotation & reuse detection)

- Each `refreshToken` is single-use: `refresh` revokes it and issues a new one.
- Reusing an **already-rotated** refresh token (a signal of theft) makes the
  API revoke the user's **entire refresh-token family** and respond `401`;
  the user must log in again. The frontend must never reuse a stale refresh
  token — always replace it with the one returned by `refresh`.
- Only the `sha256` of the token is stored at rest, never the raw token.

## Self-service (any authenticated role)

- `GET /api/v1/auth/me` → the current user.
- `PATCH /api/v1/auth/me` with `{ fullName }` → edits **only** the user's own
  name. Does not allow changing `roles`, `isActive` or `email` (admin-only
  management).
- `POST /api/v1/auth/change-password` with `{ currentPassword, newPassword }`
  → changes the user's own password after verifying the current one.
  **Revokes all of the user's refresh tokens** (closes other sessions); the
  frontend should force a re-login after a `204`.

## User management (admin)

- Users are **never deleted**: there is no `DELETE /api/v1/users/{id}` (`405`).
  A delete nulled out the author of every cut piece, activity, payment and
  history row the user left behind, and erased their attendance. To take a user
  out, `PUT /api/v1/users/{id}` with `{ isActive: false }`: login, refresh and
  any access token stop working at once, and **all of their refresh tokens are
  revoked**, so reactivating them later never brings an old session back.
- An administrator cannot lock themselves out: deactivating their own user
  (`422`, `field: "isActive"`) or dropping their own `administrador` role
  (`422`, `field: "roles"`) is refused. Since only an administrator manages
  users, there is always one active administrator left.

## Error semantics

| Code | Meaning | Frontend action |
|------|---------|------------------|
| `401 UNAUTHORIZED` | Token missing/invalid/expired, or bad credentials. | Try `refresh`; if that also `401`s, redirect to login. |
| `403 FORBIDDEN`    | Authenticated, but none of the user's **roles** grants that area. | Show "not authorized"; do not retry. |

All errors share the envelope `{ errors: [{ code, message, field? }], meta }`.

## Permission matrix by endpoint

Source of truth: `src/modules/users/permissions.py` (`RESOURCE_ROLES`). Every
route is protected with `Depends(require_permission("<key>"))`, which passes
when **any** of the user's roles is in the row.

| Area (`key`)        | administrador | vendedor | operador | canteador | proyectista | Endpoints |
|----------------------|:---:|:---:|:---:|:---:|:---:|---|
| `users:manage`       | ✅ | ❌ | ❌ | ❌ | ❌ | `/users/*` |
| `settings:manage`    | ✅ | ❌ | ❌ | ❌ | ❌ | `/settings/*` |
| `branches:manage`    | ✅ | ❌ | ❌ | ❌ | ❌ | `POST/PUT/DELETE /branches/*` |
| `branches:read`      | ✅ | ✅ | ✅ | ✅ | ✅ | `GET /branches/*` |
| `analytics`          | ✅ | ❌ | ❌ | ❌ | ❌ | `/analytics/*` |
| `inventory:check`    | ✅ | ✅ | ❌ | ❌ | ✅ | `POST /inventory/stock-check` (the alert beside a quote) |
| `inventory:low-stock` | ✅ | ❌ | ❌ | ❌ | ❌ | `GET /inventory/low-stock` (the report the admin reorders from) |
| `products:read`      | ✅ | ✅ | ❌ | ❌ | ✅ | `GET /products/*` |
| `products:write`     | ✅ | ❌ | ❌ | ❌ | ❌ | `POST/PUT/DELETE /products/*` |
| `additional_services:read`  | ✅ | ✅ | ❌ | ❌ | ❌ | `GET /additional-services/*` |
| `additional_services:write` | ✅ | ✅ | ❌ | ❌ | ❌ | `POST/PUT/DELETE /additional-services/*` |
| `clients:manage`     | ✅ | ✅ | ❌ | ❌ | ❌ | `/clients/*` |
| `optimizer`          | ✅ | ✅ | ❌ | ❌ | ✅ | `/optimize/*` (optimize, cut rules, the layout editor) |
| `optimizer:drafts`   | ✅ | ✅ | ❌ | ❌ | ❌ | `/optimization-drafts/*` (the branch's work in progress) |
| `projects:read`      | ✅ | ✅ | ❌ | ❌ | ✅** | `GET /projects`, `GET /projects/{id}` |
| `projects:write`     | ✅ | ❌ | ❌ | ❌ | ✅** | `POST /projects`, `PUT /projects/{id}`, `PATCH /projects/{id}/status` |
| `preorders`          | ✅ | ✅ | ❌ | ❌ | ❌ | `/preorders/*` (internal; the client-facing flow is public, see below) |
| `orders:read`        | ✅ | ✅ | ✅ | ❌ | ❌ | `GET /orders`, `GET /orders/{id}`, `GET /orders/{id}/document` (the order's ONLY pdf: document + diagram + annexes) |
| `orders:write`       | ✅ | ✅ | ❌ | ❌ | ❌ | `PATCH /orders/{id}/invoice`, `GET /orders/{id}/billing-export`, `PATCH /orders/{id}/branch`, `PATCH /orders/{id}/priority`, `POST/DELETE /orders/{id}/attachments[/{aid}]`, `POST /orders/{id}/payments` (a payment against the order's credit) |
| `orders:payments-void` | ✅ | ❌ | ❌ | ❌ | ❌ | `POST /orders/{id}/payments/{pid}/void` (takes a payment back off the books; the row stays) |
| `orders:transition`  | ✅ | ✅ | ✅* | ✅* | ❌ | `PATCH /orders/{id}/status` (narrowed per-transition by `TRANSITION_ROLES`) |
| `cutting_plan`       | ✅ | ✅ | ✅ | ❌ | ❌ | `GET /orders/{id}/cutting-plan` |
| `orders:cut`         | ✅ | ❌ | ✅ | ❌ | ❌ | `PATCH /orders/{id}/cutting-plan/pieces/{id}` |
| `orders:activities`  | ✅ | ❌ | ✅* | ✅* | ❌ | `PATCH /orders/{id}/activities/{type}`, `POST /orders/{id}/activities/{type}/pause` and `/resume` (narrowed per-activity by `ACTIVITY_ROLES`) |
| `orders:workshop`    | ✅ | ❌ | ✅ | ✅ | ❌ | `GET /orders/workshop-queue` (shared shop-floor board) |
| `consumables:register` | ✅ | ❌ | ✅* | ✅* | ❌ | `GET /consumables/status`, `POST /consumables/changes`, `POST /consumables/changes/{id}/void` for one's own change of a moment ago (narrowed per-consumable by `CONSUMABLE_ROLES`: the operator changes the saw and scoring blades, the `canteador` loads the glue) |
| `consumables:manage` | ✅ | ❌ | ❌ | ❌ | ❌ | `changedAt` on `POST /consumables/changes` (a change somebody forgot) and voiding any change, with a reason. The yield report is `GET /analytics/consumables` (`analytics`) |

\* Both starred rows are coarse gates, narrowed by a table in
`src/modules/orders/model.py`: `TRANSITION_ROLES` per (from, to) transition and
`ACTIVITY_ROLES` per activity (the operator cuts, the `canteador` bands and does
the additional work). `operador`/`canteador` hold `orders:transition` only for
the order's closing, which normally happens by itself when the last activity is
closed — every other transition stays shut to them, and dispatch is
admin/vendedor only.

\*\* The `proyectista` reaches only **their own** projects: another's is a
uniform `404`, never a `403`. The `vendedor` reads every project but writes
none.

### Projects (`/projects`)

A project is a cut list the `proyectista` draws with the optimizer: `name`,
`clientName` (free text: the seller picks the real client when quoting),
`notes` and an opaque `payload` (the optimizer's form state, the same contract
as a draft's). Its owner is whoever creates it.

| Status | Who moves it | To |
|--------|--------------|----|
| `in_progress` | the owner or an admin | `ready` (stamps `readyAt`), `archived` |
| `ready` | the owner or an admin | `in_progress` (reopen), `archived` |
| `archived` | the owner or an admin | `in_progress` |
| `quoted` | nobody (`PATCH /status` is a `422`) | — read-only |

- `PUT /projects/{id}` edits only an `in_progress` project (`422`,
  `field: "status"`, otherwise). There is no `DELETE`: a project is archived.
- **The hand-off**: a seller lists the queue (`GET /projects?status=ready`),
  loads the `payload` into the wizard and creates the quote with
  `POST /preorders` + `projectId`. The project must be `ready` or `quoted`
  (`422`, `field: "projectId"`, otherwise; an unknown id is a `404`), and it
  becomes `quoted` in the same commit. A `quoted` project can be quoted again,
  so one project may list several quotes (`quotes`, in the listing and in its detail). The quote
  carries `projectId`; `duplicate` does not copy it.

### Order status transitions and roles

| Transition | Allowed roles |
|------------|----------------|
| `confirmed → queued` | administrador, vendedor (requires a `payment` body, see below) |
| `confirmed → cancelled` | administrador, vendedor |
| `queued → in_process` | administrador, operador — **normally derived**: starting the `cutting` activity does it |
| `in_process → queued` (admin rollback) | administrador (reopens the cut, keeps `queuedAt`) |
| `in_process → finished` | administrador, vendedor, operador, canteador — **normally derived**: closing the last applicable activity does it. Gated on every activity being `done` |
| `finished → dispatched` | administrador, vendedor (commercial act; the shop floor cannot dispatch) |

### Activities (`PATCH /orders/{id}/activities/{type}`)

| Activity | Who | Starts when | Finishes when |
|---|---|---|---|
| `cutting` | administrador, operador | free (it IS the cut) | every piece is cut |
| `banding` | administrador, canteador | the first BANDED piece is cut | every banded piece is cut |
| `additional` | administrador, canteador | the first piece of any kind is cut | free |

Forward-only (`pending → in_progress → done`) and idempotent. The response
carries `orderStatus`, because the order may have moved by itself.

Note: `cutting` and `cut` are still valid `OrderStatus` values on **read**, in
the `history` of any order cut before the activities existed. They are not
reachable: no transition targets them.

## Public endpoints (no token)

- `GET /health`, `GET /api/v1/health/`, `/health/ready` —
  diagnostics.
- `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh`,
  `POST /api/v1/auth/logout`.
- `GET/POST /api/v1/public/review/{token}*` — the client review flow: the
  **link token** is the only credential (no JWT involved).

## Configuration (env)

| Var | Default | Description |
|-----|---------|--------------|
| `SECRET_KEY` | `dev-secret-change-me` (required in production) | Signs the JWT (HS256). |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | Access token lifetime. |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `1` | Refresh token lifetime. |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | empty | Seed the first admin account (idempotent on migration). |
