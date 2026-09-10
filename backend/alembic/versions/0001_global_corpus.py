"""Initial five-table global corpus schema."""
from alembic import op
import sqlalchemy as sa

revision = '0001_global_corpus'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('channels',
    sa.Column('platform', sa.String(length=32), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('nickname', sa.String(length=256), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('platform', 'username')
    )
    op.create_table('analyses',
    sa.Column('channel_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('video_count', sa.Integer(), nullable=False),
    sa.Column('median_views', sa.Numeric(precision=30, scale=10), nullable=False),
    sa.Column('requested_transcripts', sa.Integer(), nullable=False),
    sa.Column('completed_transcripts', sa.Integer(), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_analyses_channel_id'), 'analyses', ['channel_id'], unique=False)
    op.create_table('videos',
    sa.Column('channel_id', sa.Uuid(), nullable=False),
    sa.Column('tiktok_id', sa.String(length=30), nullable=False),
    sa.Column('author', sa.String(length=64), nullable=False),
    sa.Column('nickname', sa.String(length=256), nullable=False),
    sa.Column('caption', sa.Text(), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('duration', sa.Float(), nullable=True),
    sa.Column('url', sa.String(length=2048), nullable=False),
    sa.Column('enrichment_status', sa.String(length=16), nullable=False),
    sa.Column('enrichment_analysis_id', sa.Uuid(), nullable=True),
    sa.Column('enrichment_lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], ),
    sa.ForeignKeyConstraint(['enrichment_analysis_id'], ['analyses.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tiktok_id')
    )
    op.create_index(op.f('ix_videos_channel_id'), 'videos', ['channel_id'], unique=False)
    op.create_table('transcripts',
    sa.Column('video_id', sa.Uuid(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('language', sa.String(length=32), nullable=True),
    sa.Column('duration', sa.Float(), nullable=True),
    sa.Column('model', sa.String(length=32), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['video_id'], ['videos.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('video_id')
    )
    op.create_table('video_snapshots',
    sa.Column('analysis_id', sa.Uuid(), nullable=False),
    sa.Column('video_id', sa.Uuid(), nullable=False),
    sa.Column('views', sa.BigInteger(), nullable=False),
    sa.Column('likes', sa.BigInteger(), nullable=False),
    sa.Column('comments', sa.BigInteger(), nullable=False),
    sa.Column('shares', sa.BigInteger(), nullable=False),
    sa.Column('favorites', sa.BigInteger(), nullable=False),
    sa.Column('like_rate', sa.Numeric(precision=30, scale=10), nullable=False),
    sa.Column('comment_rate', sa.Numeric(precision=30, scale=10), nullable=False),
    sa.Column('share_rate', sa.Numeric(precision=30, scale=10), nullable=False),
    sa.Column('favorite_rate', sa.Numeric(precision=30, scale=10), nullable=False),
    sa.Column('engagement_rate', sa.Numeric(precision=30, scale=10), nullable=False),
    sa.Column('outlier_score', sa.Numeric(precision=30, scale=10), nullable=False),
    sa.Column('overall_rank', sa.Integer(), nullable=False),
    sa.Column('transcription_rank', sa.Integer(), nullable=True),
    sa.Column('transcript_eligible', sa.Boolean(), nullable=False),
    sa.Column('transcript_skip_reason', sa.String(length=32), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['analysis_id'], ['analyses.id'], ),
    sa.ForeignKeyConstraint(['video_id'], ['videos.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('analysis_id', 'video_id')
    )
    op.create_index(op.f('ix_video_snapshots_analysis_id'), 'video_snapshots', ['analysis_id'], unique=False)
    op.create_index(op.f('ix_video_snapshots_video_id'), 'video_snapshots', ['video_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_video_snapshots_video_id'), table_name='video_snapshots')
    op.drop_index(op.f('ix_video_snapshots_analysis_id'), table_name='video_snapshots')
    op.drop_table('video_snapshots')
    op.drop_table('transcripts')
    op.drop_index(op.f('ix_videos_channel_id'), table_name='videos')
    op.drop_table('videos')
    op.drop_index(op.f('ix_analyses_channel_id'), table_name='analyses')
    op.drop_table('analyses')
    op.drop_table('channels')
