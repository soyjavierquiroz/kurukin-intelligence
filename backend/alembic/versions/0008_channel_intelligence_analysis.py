"""Persist frozen structured Channel Intelligence v1 imports."""
from alembic import op
import sqlalchemy as sa


revision = '0008_channel_intel'
down_revision = '0007_semantic_viral_dna'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'channel_intelligence_analyses',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('channel_id', sa.Uuid(), nullable=False),
        sa.Column('research_pack_hash', sa.String(length=64), nullable=False),
        sa.Column('schema_version', sa.String(length=64), nullable=False),
        sa.Column('selection_mode', sa.String(length=32), nullable=False),
        sa.Column('payload_sha256', sa.String(length=64), nullable=False),
        sa.Column('channel_intelligence', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id']), sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('channel_id', 'research_pack_hash', 'schema_version',
                            name='uq_channel_intelligence_pack_schema'),
    )
    op.create_index('ix_channel_intelligence_analyses_channel_id', 'channel_intelligence_analyses', ['channel_id'])
    op.create_index('ix_channel_intelligence_analyses_research_pack_hash', 'channel_intelligence_analyses', ['research_pack_hash'])
    op.create_table(
        'channel_video_intelligence',
        sa.Column('id', sa.Uuid(), nullable=False), sa.Column('analysis_id', sa.Uuid(), nullable=False),
        sa.Column('video_id', sa.Uuid(), nullable=False), sa.Column('intelligence', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['analysis_id'], ['channel_intelligence_analyses.id']),
        sa.ForeignKeyConstraint(['video_id'], ['videos.id']), sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('analysis_id', 'video_id', name='uq_channel_video_intelligence_analysis_video'),
    )
    op.create_index('ix_channel_video_intelligence_analysis_id', 'channel_video_intelligence', ['analysis_id'])
    op.create_index('ix_channel_video_intelligence_video_id', 'channel_video_intelligence', ['video_id'])


def downgrade():
    op.drop_index('ix_channel_video_intelligence_video_id', table_name='channel_video_intelligence')
    op.drop_index('ix_channel_video_intelligence_analysis_id', table_name='channel_video_intelligence')
    op.drop_table('channel_video_intelligence')
    op.drop_index('ix_channel_intelligence_analyses_research_pack_hash', table_name='channel_intelligence_analyses')
    op.drop_index('ix_channel_intelligence_analyses_channel_id', table_name='channel_intelligence_analyses')
    op.drop_table('channel_intelligence_analyses')
