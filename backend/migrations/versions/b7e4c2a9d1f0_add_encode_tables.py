"""add encode_profiles, encode_tasks, encode_settings

Revision ID: b7e4c2a9d1f0
Revises: c6e3a8d2f5b1
Create Date: 2026-11-09 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

from app.migration_guards import table_exists

# revision identifiers, used by Alembic.
revision: str = "b7e4c2a9d1f0"
down_revision: str | Sequence[str] | None = "c6e3a8d2f5b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("encode_profiles", "encode_settings", "encode_tasks")  # FK order


def upgrade() -> None:
    # create_all() in init_db() normally makes these from the models before
    # Alembic runs; only create them on a DB that lacks them. Creating from the
    # model's own Table keeps the two paths from drifting apart.
    from sqlmodel import SQLModel

    import app.models.encode  # noqa: F401  (registers the tables on the metadata)

    bind = op.get_bind()
    for name in _TABLES:
        if not table_exists(name):
            SQLModel.metadata.tables[name].create(bind)


def downgrade() -> None:
    for name in reversed(_TABLES):
        if table_exists(name):
            op.drop_table(name)
