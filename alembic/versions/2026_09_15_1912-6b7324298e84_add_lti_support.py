"""add lti support

Revision ID: 6b7324298e84
Revises: 8a6766f6326b
Create Date: 2026-09-15 19:12:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6b7324298e84'
down_revision: Union[str, None] = '8a6766f6326b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('lti_iss', sa.String(), nullable=True))
    op.add_column('users', sa.Column('lti_sub', sa.String(), nullable=True))
    op.create_unique_constraint('uq_users_lti_identity', 'users', ['lti_iss', 'lti_sub'])

    op.add_column('courses', sa.Column('lti_context_id', sa.String(), nullable=True))
    op.create_index(op.f('ix_courses_lti_context_id'), 'courses', ['lti_context_id'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_courses_lti_context_id'), table_name='courses')
    op.drop_column('courses', 'lti_context_id')

    op.drop_constraint('uq_users_lti_identity', 'users', type_='unique')
    op.drop_column('users', 'lti_sub')
    op.drop_column('users', 'lti_iss')
