"""Record, per role, whether the candidate counts it toward "years of experience".

Step 9c prints a years figure in the summary. Until now that figure came from the role's
kind, which the importer proposes from the title and employer: exactly the inference that
goes wrong for student and part-time roles (DECISIONS 2026-10-25). The candidate now says
it for each role. NULL means nobody has said yet, and while any approved role is NULL the
engine makes no years claim at all; it asks instead.

The question is a new kind, so the user_questions kind CHECK from 0003 is rebuilt with it.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The kinds 0003 allowed, as 0003 wrote them; this migration adds one.
KINDS_BEFORE = (
    "missing_dates",
    "paid_or_unpaid",
    "employer_or_title",
    "columns_merged",
    "unknown_section",
    "unknown_skills",
    "possible_duplicate",
    "missing_metric",
    "missing_context",
    "unevidenced_skill",
    "missing_profile_field",
    "motivation",
)
KINDS_AFTER = (*KINDS_BEFORE, "counts_toward_years")
KIND_CHECK = "ck_user_questions_kind_is_known"


def _kind_check(kinds: tuple[str, ...]) -> None:
    # op.f: the name is already final; without it the naming convention prefixes it again.
    op.drop_constraint(op.f(KIND_CHECK), "user_questions", type_="check")
    op.create_check_constraint(
        op.f(KIND_CHECK), "user_questions", "kind IN (" + ", ".join(f"'{kind}'" for kind in kinds) + ")"
    )


def upgrade() -> None:
    op.add_column("experiences", sa.Column("counts_toward_years", sa.Boolean(), nullable=True))
    _kind_check(KINDS_AFTER)


def downgrade() -> None:
    op.execute("DELETE FROM user_questions WHERE kind = 'counts_toward_years'")
    _kind_check(KINDS_BEFORE)
    op.drop_column("experiences", "counts_toward_years")
