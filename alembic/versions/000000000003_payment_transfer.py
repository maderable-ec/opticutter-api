"""Bank transfer as a third payment method

The ``confirmed → queued`` gate only accepted cash and credit, so a transfer was
registered as cash and the "FORMA DE PAGO" block of the ORDEN DE PEDIDO and the
HOJA DE DESPACHO printed the wrong method. Additive and informational: the
payment never touches pricing or billing, and the method used is still inferred
from which amount is > 0, so a pre-feature order simply leaves the column NULL.

Revision ID: 000000000003
Revises: 000000000002
Create Date: 2026-09-02

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "000000000003"
down_revision: Union[str, None] = "000000000002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable like its two siblings: NULL means "not paid this way", which is
    # what every order registered before this migration says.
    op.add_column(
        "orders", sa.Column("payment_transfer_amount", sa.Float(), nullable=True)
    )
    op.create_check_constraint(
        "payment_transfer_non_negative", "orders", "payment_transfer_amount >= 0"
    )


def downgrade() -> None:
    op.drop_constraint("payment_transfer_non_negative", "orders", type_="check")
    op.drop_column("orders", "payment_transfer_amount")
