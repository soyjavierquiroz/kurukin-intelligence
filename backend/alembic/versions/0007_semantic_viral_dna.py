"""Add the frozen v1 global Semantic Viral DNA contract.

This revision has not been applied in production.  It intentionally replaced
the pre-freeze draft in place; the later, independent Channel Intelligence
tables therefore begin at revision 0008.
"""
from alembic import op
import sqlalchemy as sa

from app.llm.semantic_contract import (
    SEMANTIC_ENUM_FIELDS,
    SEMANTIC_OUTPUT_FIELDS,
    SEMANTIC_SECONDARY_PRIMARY_PAIRS,
    SEMANTIC_TEXT_FIELDS,
    semantic_secondary_constraint_name,
)


revision = '0007_semantic_viral_dna'
down_revision = '0006_viral_dna'
branch_labels = None
depends_on = None


def _enum_constraint_sql(field: str) -> str:
    values = ','.join(repr(value) for value in SEMANTIC_ENUM_FIELDS[field])
    return f'{field} IN ({values})'


def _semantic_column(field: str) -> sa.Column:
    length = SEMANTIC_TEXT_FIELDS.get(field, 32)
    return sa.Column(field, sa.String(length=length), nullable=True)


def upgrade():
    # batch_alter_table keeps this migration executable in SQLite migration
    # contract tests while emitting normal ALTERs on PostgreSQL.  No indexes
    # are added yet; the values remain normal SQL columns for future analytics.
    with op.batch_alter_table('viral_dna') as batch:
        for field in SEMANTIC_OUTPUT_FIELDS:
            batch.add_column(_semantic_column(field))
        batch.add_column(sa.Column('semantic_input_sha256', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('semantic_model', sa.String(length=128), nullable=True))
        batch.add_column(sa.Column('semantic_prompt_version', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('semantic_extracted_at', sa.DateTime(timezone=True), nullable=True))
        for field in SEMANTIC_ENUM_FIELDS:
            batch.create_check_constraint(f'ck_viral_dna_{field}', _enum_constraint_sql(field))
        for primary, secondary in SEMANTIC_SECONDARY_PRIMARY_PAIRS:
            batch.create_check_constraint(
                semantic_secondary_constraint_name(primary, secondary),
                f'{secondary} IS NULL OR {primary} != {secondary}',
            )


def downgrade():
    with op.batch_alter_table('viral_dna') as batch:
        for primary, secondary in reversed(SEMANTIC_SECONDARY_PRIMARY_PAIRS):
            batch.drop_constraint(
                semantic_secondary_constraint_name(primary, secondary), type_='check'
            )
        for field in reversed(tuple(SEMANTIC_ENUM_FIELDS)):
            batch.drop_constraint(f'ck_viral_dna_{field}', type_='check')
        batch.drop_column('semantic_extracted_at')
        batch.drop_column('semantic_prompt_version')
        batch.drop_column('semantic_model')
        batch.drop_column('semantic_input_sha256')
        for field in reversed(SEMANTIC_OUTPUT_FIELDS):
            batch.drop_column(field)
