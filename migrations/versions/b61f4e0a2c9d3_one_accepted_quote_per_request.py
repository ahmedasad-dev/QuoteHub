"""Enforce at most one accepted quote per request in PostgreSQL.

Revision ID: b61f4e0a2c9d3
Revises: 7c504dafc2a9
Create Date: 2026-10-31
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b61f4e0a2c9d3"
down_revision: Union[str, None] = "7c504dafc2a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "uq_quotes_one_accepted_per_request"


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return

    duplicate = connection.execute(sa.text(
        "SELECT service_request_id, count(*) AS accepted_count "
        "FROM quotes WHERE status = 'ACCEPTED' "
        "GROUP BY service_request_id HAVING count(*) > 1 "
        "ORDER BY service_request_id LIMIT 1"
    )).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot add the accepted-quote uniqueness rule: service request "
            f"{duplicate.service_request_id} has {duplicate.accepted_count} accepted quotes. "
            "Reconcile the existing quote statuses, then rerun the migration."
        )

    op.create_index(
        INDEX_NAME,
        "quotes",
        ["service_request_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACCEPTED'"),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index(INDEX_NAME, table_name="quotes")
