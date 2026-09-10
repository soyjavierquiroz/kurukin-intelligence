"""Durable operational transcription jobs; original corpus is unchanged."""
from alembic import op
import sqlalchemy as sa

revision = '0002_async_transcription_jobs'
down_revision = '0001_global_corpus'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('transcription_jobs',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('video_id', sa.Uuid(), sa.ForeignKey('videos.id'), nullable=False, unique=True),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='50'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('audio_path', sa.String(1024)),
        sa.Column('audio_size_bytes', sa.BigInteger()),
        sa.Column('audio_duration', sa.Float()),
        *[sa.Column(name, sa.DateTime(timezone=True), nullable=name not in ('created_at', 'updated_at'))
          for name in ('reserved_at', 'audio_received_at', 'queued_at', 'started_at', 'completed_at',
                       'failed_at', 'created_at', 'updated_at')],
        sa.Column('last_error_code', sa.String(64)),
        sa.CheckConstraint("status IN ('reserved','audio_received','queued','processing','completed','failed','expired')", name='ck_job_status'))
    op.create_index('ix_transcription_jobs_status', 'transcription_jobs', ['status'])


def downgrade():
    op.drop_index('ix_transcription_jobs_status', table_name='transcription_jobs')
    op.drop_table('transcription_jobs')
