# Multi-branch — API contract

Cutter isolates **orders, pre-orders and optimization drafts** by branch.
This document summarizes what that means for the frontend (React dashboard).
Auth details live in [`AUTH.md`](AUTH.md).

## Model

- **Branch** (`branch`) is a real entity (`/branches`). It replaced the
  letterhead JSON that used to live in `settings.company_branches`.
- Every staff **user** with role `vendedor`/`operador` has **one** base
  branch (`branchId`). The **operador** is bound to it (only sees/operates
  that branch). The **vendedor** and **administrador** are **global**: they
  see and operate every branch. A vendedor's `branchId` is just their
  **default** branch (set at creation, overridable per request); an
  administrador's is always null.
- **Orders, pre-orders and drafts** each store their own `branchId`. It's a
  historical fact: reassigning a user's branch does **not** move their past
  documents. Their responses (detail and list) **embed** the owning branch
  as a compact object, `branch: { id, code, name }`, so the dashboard can
  show a "Branch" column without an extra request.
- **Clients and products stay global** (a single shared portfolio/catalog).
- A branch optionally carries `warehouseCode`, the vendor's warehouse its
  **stock** lives in (SIFAC `mbodega.cod`). It is what makes the low-stock alert
  and the low-stock report per-branch. `null` means the branch does not consult
  stock: no alert while quoting, no rows of its own in the report. A dedicated
  field and not `code`, which the admin can rename at will.

## Isolation (who sees/does what)

| Role | List & access | Create |
|------|----------------|--------|
| `administrador` | **All** branches; optional `?branchId=` filter | Must specify `branchId` |
| `vendedor` | **All** branches; optional `?branchId=` filter | Defaults to their base branch; can override with `branchId` |
| `operador` | Only **their** branch | Inherits their branch (`branchId` in the body is ignored) |

A resource belonging to another branch returns **404**, not 403 — its
existence is not revealed. (`operador` doesn't create
orders/pre-orders/drafts; its "Create" column follows the generic rule.)

## New endpoints

### `/branches` (CRUD; only admin writes, any staff role reads)

- `POST /api/v1/branches` — `{ code, name, address?, phone?, warehouseCode? }`
- `GET /api/v1/branches` — paginated, `?search=`
- `GET /api/v1/branches/{id}`
- `PUT /api/v1/branches/{id}` — includes `isActive` (soft delete)
- `DELETE /api/v1/branches/{id}`

### Per-branch analytics (admin only)

- `GET /api/v1/analytics/breakdown/branch` — comparison (count + revenue) per branch.
- Every analytics endpoint accepts `?branchId=` to scope a single branch.
- `GET /api/v1/analytics/low-stock` — boards and edge bandings under their
  configured threshold, `?branchId=` + `?type=board|edge_banding`. The only
  analytics endpoint with **no date range** (stock is a state, not a series) and
  the only one reading an external source; `checked: false` means the vendor's
  system did not answer, which is not the same as "nothing is low".

## Changes to existing endpoints

- **Users** (`POST/PUT /users`, login response): the user body/object
  includes `branchId`. Required for `vendedor`/`operador`; ignored for
  `administrador` (stays null). Show a branch selector when creating/editing
  staff.
- **Pre-orders** (`POST /preorders`): accepts `branchId`. `operador` ignores
  it (inherits their own); `vendedor` may omit it (falls back to their base
  branch) or send it to create in another branch; `administrador` must send
  it. `GET /preorders` accepts `?branchId=` for the global roles
  (admin/vendedor).
- **Orders** (`GET /orders`): accepts `?branchId=` for the global roles
  (admin/vendedor). An order is born with its pre-order's branch on
  confirmation; the letterhead of its document shows that branch.
- **Drafts** (`POST /optimization-drafts`): accepts `branchId` (same rule as
  pre-orders); `GET` accepts `?branchId=` (admin/vendedor).

## Relevant errors

- `422 VALIDATION_ERROR` (`field: "branchId"`): an admin creating a resource
  without specifying a branch, or specifying an inactive one.
- `403 FORBIDDEN`: an `operador` with no assigned branch (an invalid state
  that an admin must fix).
- `404 NOT_FOUND`: accessing a resource that belongs to another branch
  (uniform with "does not exist").

## Stock by branch

- `POST /api/v1/inventory/stock-check` (admin + seller) answers, for ONE branch,
  which of the products a quote consumes are below their threshold or short for
  the job: `{ branchId, items: [{ productId, quantity }] }`. Quantities are the
  units the warehouse counts — sheets for a board, linear metres for edge
  banding — which is exactly what `materialsSummary[].count`,
  `edgeBandingsSummary[].billedLinearM` and `orderLines[].quantity` already are.
- Materials with no `productId` (a client's offcut, a "medida manual") are not
  stocked anywhere and are simply left out of `items`.
- It **never** blocks anything. `checked: false` (branch with no
  `warehouseCode`, or the vendor's database unreachable) comes back `200` with
  no alerts, because this is information beside a quote.
- Thresholds are global per product type, not per branch:
  `GET/PATCH /api/v1/settings/stock` (`{ board, edgeBanding }`, admin only).
