"""encode_tasks: encoder and bitrate columns (fork, encode stage)

Revision ID: e3a91c5d7b20
Revises: b7e4c2a9d1f0
Create Date: 2026-11-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from app.migration_guards import add_column_if_missing, drop_column_if_exists

revision: str = "e3a91c5d7b20"
down_revision: str | Sequence[str] | None = "b7e4c2a9d1f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("encoder", sa.String()),
    ("source_video_kbps", sa.Integer()),
    ("target_kbps", sa.Integer()),
    ("peak_kbps", sa.Integer()),
    ("quality", sa.Integer()),
    ("current_kbps", sa.Integer()),
    ("output_video_kbps", sa.Integer()),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        add_column_if_missing("encode_tasks", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        drop_column_if_exists("encode_tasks", name)
