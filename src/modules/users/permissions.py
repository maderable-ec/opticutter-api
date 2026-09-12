"""Per-role authorization matrix (enforcement-phase spec).

Source of truth for which role can access which area. In the enforcement phase, each
route is protected with ``require_role(*RESOURCE_ROLES[key])`` (see ``dependencies.py``).

| Area                          | administrador | vendedor | operador | canteador |
|-------------------------------|---------------|----------|----------|-----------|
| users:manage                  | yes           | no       | no       | no        |
| settings:manage               | yes           | no       | no       | no        |
| branches:manage               | yes           | no       | no       | no        |
| branches:read                 | yes           | yes      | yes      | yes       |
| clients:manage                | yes           | yes      | no       | no        |
| products:write                | yes           | no       | no       | no        |
| products:sync                 | yes           | yes      | no       | no        |
| products:read                 | yes           | yes      | no       | no        |
| additional_services:write     | yes           | yes      | no       | no        |
| additional_services:read      | yes           | yes      | no       | no        |
| optimizer (optimizations/drafts) | yes        | yes      | no       | no        |
| preorders                     | yes           | yes      | no       | no        |
| orders:write (create/quote)   | yes           | yes      | no       | no        |
| orders:read                   | yes           | yes      | yes      | no        |
| orders:transition (status change) | yes       | yes      | yes*     | yes*      |
| orders:workshop (shop board)  | yes           | no       | yes      | yes       |
| cutting_plan (view plan)      | yes           | yes      | yes      | no        |
| orders:cut (mark pieces)      | yes           | no       | yes      | no        |
| orders:activities (work)      | yes           | no       | yes*     | yes*      |
| analytics                     | yes           | no       | no       | no        |
| notifications:read            | yes           | yes      | yes      | yes       |
| print:agents (register/token) | yes           | no       | no       | no        |

* Two layers, in both rows. The area opens the endpoint; a table in
  ``orders/model.py`` decides the specific move: TRANSITION_ROLES per (from, to)
  transition, ACTIVITY_ROLES per activity (the operator cuts, the bander bands and
  does the additional work). The shop floor holds ``orders:transition`` only for
  the order's derived closing; dispatch (``finished -> dispatched``) is a commercial
  act restricted to admin/seller, and every other transition stays off-limits.

The bander doesn't see order detail (no ``orders:read``): only the activity
endpoint (``orders:activities``) and the self-sufficient shop-floor board
(``orders:workshop``), which is where they work from.
"""

from src.modules.users.enums import UserRole

_ADMIN = UserRole.ADMIN
_SELLER = UserRole.SELLER
_OPERATOR = UserRole.OPERATOR
_BANDER = UserRole.BANDER

RESOURCE_ROLES: dict[str, tuple[UserRole, ...]] = {
    "users:manage": (_ADMIN,),
    "settings:manage": (_ADMIN,),
    # Branches: only the admin manages them (CRUD). Any staff needs read access
    # to populate selectors and show their branch's name.
    "branches:manage": (_ADMIN,),
    "branches:read": (_ADMIN, _SELLER, _OPERATOR, _BANDER),
    "clients:manage": (_ADMIN, _SELLER),
    "products:write": (_ADMIN,),
    # Catalog sync from the vendor's inventory: deliberately NOT products:write.
    # The seller needs to pull fresh prices before quoting (it is the step that
    # loads price_2/price_3), but must not be able to create, edit or delete a
    # product. The sync is idempotent and has ?dryRun=true; what it does grant is
    # the reconciliation pass, which deactivates products the source stopped
    # bringing.
    "products:sync": (_ADMIN, _SELLER),
    "products:read": (_ADMIN, _SELLER),
    # Additional-services catalog: admin and seller both manage it (create,
    # edit, delete) — the seller registers the services they quote.
    "additional_services:write": (_ADMIN, _SELLER),
    "additional_services:read": (_ADMIN, _SELLER),
    "optimizer": (_ADMIN, _SELLER),
    "preorders": (_ADMIN, _SELLER),
    "orders:write": (_ADMIN, _SELLER),
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
    "analytics": (_ADMIN,),
    # Any authenticated role reads/acks its own notifications; the service scopes
    # every query to the current user's id.
    "notifications:read": (_ADMIN, _SELLER, _OPERATOR, _BANDER),
    # Print-agent registration + token issuance (one agent per branch shop PC).
    # The label/consolidated *enqueue* endpoints reuse orders:cut / orders:workshop;
    # the agent's own long-poll endpoints authenticate with a device token, not a role.
    "print:agents": (_ADMIN,),
}
