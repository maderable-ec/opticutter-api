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

`operador` and `canteador` are workshop roles bound to a single branch;
`administrador` and `vendedor` are global (see
[`MULTI_BRANCH.md`](MULTI_BRANCH.md)).

Login is by `email` (unique). Passwords are stored only as a bcrypt hash.

## Session flow

1. **Login** → `POST /api/v1/auth/login` with `{ email, password }`.
   Response: `{ accessToken, refreshToken, tokenType: "bearer", expiresIn, user }`.
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
  name. Does not allow changing `role`, `isActive` or `email` (admin-only
  management).
- `POST /api/v1/auth/change-password` with `{ currentPassword, newPassword }`
  → changes the user's own password after verifying the current one.
  **Revokes all of the user's refresh tokens** (closes other sessions); the
  frontend should force a re-login after a `204`.

## Error semantics

| Code | Meaning | Frontend action |
|------|---------|------------------|
| `401 UNAUTHORIZED` | Token missing/invalid/expired, or bad credentials. | Try `refresh`; if that also `401`s, redirect to login. |
| `403 FORBIDDEN`    | Authenticated, but the **role** lacks permission for that area. | Show "not authorized"; do not retry. |

All errors share the envelope `{ errors: [{ code, message, field? }], meta }`.

## Permission matrix by endpoint

Source of truth: `src/modules/users/permissions.py` (`RESOURCE_ROLES`). Every
route is protected with `Depends(require_permission("<key>"))`.

| Area (`key`)        | administrador | vendedor | operador | canteador | Endpoints |
|----------------------|:---:|:---:|:---:|:---:|---|
| `users:manage`       | ✅ | ❌ | ❌ | ❌ | `/users/*` |
| `settings:manage`    | ✅ | ❌ | ❌ | ❌ | `/settings/*` |
| `branches:manage`    | ✅ | ❌ | ❌ | ❌ | `POST/PUT/DELETE /branches/*` |
| `branches:read`      | ✅ | ✅ | ✅ | ✅ | `GET /branches/*` |
| `analytics`          | ✅ | ❌ | ❌ | ❌ | `/analytics/*` |
| `products:read`      | ✅ | ✅ | ❌ | ❌ | `GET /products/*` |
| `products:write`     | ✅ | ❌ | ❌ | ❌ | `POST/PUT/DELETE /products/*` |
| `additional_services:read`  | ✅ | ✅ | ❌ | ❌ | `GET /additional-services/*` |
| `additional_services:write` | ✅ | ✅ | ❌ | ❌ | `POST/PUT/DELETE /additional-services/*` |
| `clients:manage`     | ✅ | ✅ | ❌ | ❌ | `/clients/*` |
| `optimizer`          | ✅ | ✅ | ❌ | ❌ | `/optimize/*`, `/optimization-drafts/*` |
| `preorders`          | ✅ | ✅ | ❌ | ❌ | `/preorders/*` (internal; the client-facing flow is public, see below) |
| `orders:read`        | ✅ | ✅ | ✅ | ❌ | `GET /orders`, `GET /orders/{id}`, `GET /orders/{id}/document`, `GET /orders/{id}/dispatch-sheet` |
| `orders:write`       | ✅ | ✅ | ❌ | ❌ | `POST /orders/{id}/invoice`, `GET /orders/{id}/export`, `POST/DELETE /orders/{id}/attachments[/{aid}]` |
| `orders:transition`  | ✅ | ✅ | ✅* | ✅* | `PATCH /orders/{id}/status` (narrowed per-transition by `TRANSITION_ROLES`) |
| `cutting_plan`       | ✅ | ✅ | ✅ | ❌ | `GET /orders/{id}/cutting-plan`, `GET /orders/{id}/production-sheet` |
| `orders:cut`         | ✅ | ❌ | ✅ | ❌ | `PATCH /orders/{id}/cutting-plan/pieces/{id}` |
| `orders:band`        | ✅ | ❌ | ❌ | ✅ | `PATCH /orders/{id}/banding` |
| `orders:workshop`    | ✅ | ❌ | ✅ | ✅ | `GET /orders/workshop-queue` (shared shop-floor board) |

\* `orders:transition` is the coarse gate; which role can perform each
*specific* transition lives in `TRANSITION_ROLES`
(`src/modules/orders/model.py`). `operador`/`canteador` get into this permission
to **complete** orders (`cut → completed`) from the shop-floor board — every
other transition stays closed to them (dispatch is admin/vendedor only).

### Order status transitions and roles

| Transition | Allowed roles |
|------------|----------------|
| `confirmed → queued` | administrador, vendedor (requires a `payment` body, see below) |
| `confirmed → cancelled` | administrador, vendedor |
| `queued → cutting` | administrador, operador |
| `cutting → queued` (admin rollback) | administrador |
| `cutting → cut` | administrador, operador |
| `cut → completed` | administrador, vendedor, operador, canteador (Gate B: banding must be `done`) |
| `completed → dispatched` | administrador, vendedor (commercial act; the shop floor cannot dispatch) |

Note: the order status wire value for the final state is the literal string
`"despachado"` (`OrderStatus.dispatched`), kept in Spanish for backward
compatibility with the dashboard.

## Public endpoints (no token)

- `GET /health`, `GET /api/v1/health/`, `/health/ready`, `/api/v1/cutter/*` —
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
