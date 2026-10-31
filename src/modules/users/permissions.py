"""Per-role authorization matrix (enforcement-phase spec).

Source of truth for which role can access which area. In the enforcement phase, each
route is protected with ``require_role(*RESOURCE_ROLES[key])`` (see ``dependencies.py``).

| Area                          | administrador | vendedor | operador | canteador | proyectista |
|-------------------------------|---------------|----------|----------|-----------|-------------|
| users:manage                  | yes           | no       | no       | no        | no          |
| settings:manage               | yes           | no       | no       | no        | no          |
| branches:manage               | yes           | no       | no       | no        | no          |
| branches:read                 | yes           | yes      | yes      | yes       | yes         |
| clients:manage                | yes           | yes      | no       | no        | no          |
| products:write                | yes           | no       | no       | no        | no          |
| products:sync                 | yes           | yes      | no       | no        | no          |
| products:read                 | yes           | yes      | no       | no        | yes         |
| additional_services:write     | yes           | yes      | no       | no        | no          |
| additional_services:read      | yes           | yes      | no       | no        | no          |
| optimizer (/optimize)         | yes           | yes      | no       | no        | yes         |
| optimizer:drafts              | yes           | yes      | no       | no        | no          |
| projects:read                 | yes           | yes      | no       | no        | yes**       |
| projects:write                | yes           | no       | no       | no        | yes**       |
| preorders                     | yes           | yes      | no       | no        | no          |
| orders:write (create/quote)   | yes           | yes      | no       | no        | no          |
| orders:payments-void          | yes           | no       | no       | no        | no          |
| orders:read                   | yes           | yes      | yes      | no        | no          |
| orders:transition (status change) | yes       | yes      | yes*     | yes*      | no          |
| orders:workshop (shop board)  | yes           | no       | yes      | yes       | no          |
| cutting_plan (view plan)      | yes           | yes      | yes      | no        | no          |
| orders:cut (mark pieces)      | yes           | no       | yes      | no        | no          |
| orders:activities (work)      | yes           | no       | yes*     | yes*      | no          |
| inventory:check (quoting)     | yes           | yes      | no       | no        | yes         |
| inventory:low-stock (report)  | yes           | no       | no       | no        | no          |
| consumables:register (blades, glue) | yes     | no       | yes*     | yes*      | no          |
| consumables:manage (corrections) | yes        | no       | no       | no        | no          |
| analytics                     | yes           | no       | no       | no        | no          |
| notifications:read            | yes           | yes      | yes      | yes       | yes         |
| print:agents (register/token) | yes           | no       | no       | no        | no          |

* Two layers, in both rows. The area opens the endpoint; a table in
  ``orders/model.py`` decides the specific move: TRANSITION_ROLES per (from, to)
  transition, ACTIVITY_ROLES per activity (the operator cuts, the bander bands and
  does the additional work). The shop floor holds ``orders:transition`` only for
  the order's derived closing; dispatch (``finished -> dispatched``) is a commercial
  act restricted to admin/seller, and every other transition stays off-limits.
  Cancelling narrows further as the order advances: from ``confirmed`` it is
  admin/seller (the quote died), but from ``queued`` -- already paid for -- it
  is ADMIN ONLY, so the seller who raised it cannot undo a collected sale.

``consumables:register`` follows the same scheme: ``CONSUMABLE_ROLES`` in
``consumables/model.py`` narrows it per consumable.

The bander doesn't see order detail (no ``orders:read``): only the activity
endpoint (``orders:activities``) and the self-sufficient shop-floor board
(``orders:workshop``), which is where they work from.

** The designer reads and writes only THEIR OWN projects: ``ProjectService``
scopes by owner (another's project is a uniform 404). The seller reads every
project to quote it, and quoting is ``preorders``, which the designer lacks: the
hand-off is the seller's.
"""

from src.modules.users.enums import UserRole

_ADMIN = UserRole.ADMIN
_SELLER = UserRole.SELLER
_OPERATOR = UserRole.OPERATOR
_BANDER = UserRole.BANDER
_DESIGNER = UserRole.DESIGNER

RESOURCE_ROLES: dict[str, tuple[UserRole, ...]] = {
    "users:manage": (_ADMIN,),
    "settings:manage": (_ADMIN,),
    # Branches: only the admin manages them (CRUD). Any staff needs read access
    # to populate selectors and show their branch's name.
    "branches:manage": (_ADMIN,),
    "branches:read": (_ADMIN, _SELLER, _OPERATOR, _BANDER, _DESIGNER),
    "clients:manage": (_ADMIN, _SELLER),
    "products:write": (_ADMIN,),
    # Catalog sync from the vendor's inventory: deliberately NOT products:write.
    # The seller needs to pull fresh prices before quoting (it is the step that
    # loads price_2/price_3), but must not be able to create, edit or delete a
    # product. The sync is idempotent and has ?dryRun=true; what it does grant is
    # the reconciliation pass, which deactivates products the source stopped
    # bringing.
    "products:sync": (_ADMIN, _SELLER),
    # The designer reads the catalog the optimizer's wizard is built from
    # (boards, the edge bandings that fit one, families).
    "products:read": (_ADMIN, _SELLER, _DESIGNER),
    # Additional-services catalog: admin and seller both manage it (create,
    # edit, delete) — the seller registers the services they quote.
    "additional_services:write": (_ADMIN, _SELLER),
    "additional_services:read": (_ADMIN, _SELLER),
    # The computation itself (``/optimize``: optimize, cut rules, the layout
    # editor). The designer works in it; the drafts are a separate area below.
    "optimizer": (_ADMIN, _SELLER, _DESIGNER),
    # The branch's work in progress. Not the designer's: their projects replace
    # it, and they are scoped by owner, not by branch.
    "optimizer:drafts": (_ADMIN, _SELLER),
    # The designer's projects. The service narrows the designer to their own;
    # the seller reads them all, to quote the ready ones, but never edits one.
    "projects:read": (_ADMIN, _SELLER, _DESIGNER),
    "projects:write": (_ADMIN, _DESIGNER),
    "preorders": (_ADMIN, _SELLER),
    "orders:write": (_ADMIN, _SELLER),
    # Registering a payment on an order is ``orders:write`` (the seller collects).
    # Voiding one takes money back off the books, so it is the admin's alone.
    "orders:payments-void": (_ADMIN,),
    "orders:read": (_ADMIN, _SELLER, _OPERATOR),
    "orders:transition": (_ADMIN, _SELLER, _OPERATOR, _BANDER),
    # Shared shop-floor board (self-sufficient card list). The bander reaches it
    # despite lacking ``orders:read``: it embeds the client + board names.
    "orders:workshop": (_ADMIN, _OPERATOR, _BANDER),
    "cutting_plan": (_ADMIN, _SELLER, _OPERATOR),
    "orders:cut": (_ADMIN, _OPERATOR),
    # The shop floor's work endpoint (start/finish an activity). Deliberately one
    # area for the three activities: ACTIVITY_ROLES is what says the operator cuts
    # and the bander bands, and splitting it per activity would duplicate that
    # decision in two places.
    "orders:activities": (_ADMIN, _OPERATOR, _BANDER),
    # The stock question asked while building a cut list (the seller quoting,
    # the designer drawing a project): it is part of the work, not a report.
    "inventory:check": (_ADMIN, _SELLER, _DESIGNER),
    # The low-stock report the admin reorders from (GET /inventory/low-stock).
    # Admin only, as it was while it hung off /analytics.
    "inventory:low-stock": (_ADMIN,),
    # The shop's consumables (blades, glue): reading what is on the machine and
    # registering a change. One area for the three, like ``orders:activities``:
    # ``CONSUMABLE_ROLES`` (consumables/model.py) says the operator changes the
    # blades and the bander loads the glue.
    "consumables:register": (_ADMIN, _OPERATOR, _BANDER),
    # Correcting the record: entering a change somebody forgot, and voiding one
    # that is not the caller's own of a moment ago.
    "consumables:manage": (_ADMIN,),
    "analytics": (_ADMIN,),
    # Any authenticated role reads/acks its own notifications; the service scopes
    # every query to the current user's id.
    "notifications:read": (_ADMIN, _SELLER, _OPERATOR, _BANDER, _DESIGNER),
    # Print-agent registration + token issuance (one agent per branch shop PC).
    # The label/consolidated *enqueue* endpoints reuse orders:cut / orders:workshop;
    # the agent's own long-poll endpoints authenticate with a device token, not a role.
    "print:agents": (_ADMIN,),
}
