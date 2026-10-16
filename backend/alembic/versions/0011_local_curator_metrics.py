"""Independent partial local-curator metric observations; no scan membership."""
from alembic import op
import sqlalchemy as sa

revision = '0011_local_metrics'
down_revision = '0010_knowledge_strategist'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'video_metric_observations',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('video_id', sa.Uuid(), nullable=False),
        sa.Column('source', sa.String(32), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('views', sa.BigInteger(), nullable=True),
        sa.Column('likes', sa.BigInteger(), nullable=True),
        sa.Column('bundle_sha256', sa.String(64), nullable=True),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['video_id'], ['videos.id'], ondelete='CASCADE'),
        sa.CheckConstraint('views IS NULL OR views >= 0', name='ck_local_metrics_views'),
        sa.CheckConstraint('likes IS NULL OR likes >= 0', name='ck_local_metrics_likes'),
        sa.CheckConstraint('views IS NOT NULL OR likes IS NOT NULL', name='ck_local_metrics_known'),
        sa.UniqueConstraint('video_id', 'source', 'fingerprint', name='uq_local_metric_observation'),
    )
    op.create_index('ix_video_metric_observations_video_observed', 'video_metric_observations', ['video_id', 'observed_at'])


def downgrade():
    op.drop_index('ix_video_metric_observations_video_observed', table_name='video_metric_observations')
    op.drop_table('video_metric_observations')
