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

- `POST /api/v1/branches` — `{ code, name, address?, phone? }`
- `GET /api/v1/branches` — paginated, `?search=`
- `GET /api/v1/branches/{id}`
- `PUT /api/v1/branches/{id}` — includes `isActive` (soft delete)
- `DELETE /api/v1/branches/{id}`

### Per-branch analytics (admin only)

- `GET /api/v1/analytics/breakdown/branch` — comparison (count + revenue) per branch.
- Every analytics endpoint accepts `?branchId=` to scope a single branch.

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
