"""Persist private content packs separately from global channel intelligence."""
from alembic import op
import sqlalchemy as sa


revision = '0009_private_content_packs'
down_revision = '0008_channel_intel'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'private_content_packs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('channel_id', sa.Uuid(), nullable=False),
        sa.Column('analysis_id', sa.Uuid(), nullable=False),
        sa.Column('payload_sha256', sa.String(length=64), nullable=False),
        sa.Column('private_context', sa.JSON(), nullable=False),
        sa.Column('content_pack', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id']),
        sa.ForeignKeyConstraint(['analysis_id'], ['channel_intelligence_analyses.id']),
        sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('payload_sha256'),
    )
    op.create_index('ix_private_content_packs_channel_id', 'private_content_packs', ['channel_id'])
    op.create_index('ix_private_content_packs_analysis_id', 'private_content_packs', ['analysis_id'])


def downgrade():
    op.drop_index('ix_private_content_packs_analysis_id', table_name='private_content_packs')
    op.drop_index('ix_private_content_packs_channel_id', table_name='private_content_packs')
    op.drop_table('private_content_packs')
