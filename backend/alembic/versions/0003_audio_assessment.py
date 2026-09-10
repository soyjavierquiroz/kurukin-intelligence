"""Global server-side YAMNet assessments and terminal music resolution."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003_audio_assessment'
down_revision = '0002_async_transcription_jobs'
branch_labels = None
depends_on = None


JOB_STATUS = "status IN ('reserved','audio_received','queued','processing','completed','failed','expired','skipped')"
OLD_STATUS = "status IN ('reserved','audio_received','queued','processing','completed','failed','expired')"


def upgrade():
    op.create_table('audio_assessments',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('video_id', sa.Uuid(), sa.ForeignKey('videos.id'), nullable=False),
        sa.Column('classifier', sa.String(64), nullable=False),
        sa.Column('classifier_version', sa.String(64), nullable=False),
        sa.Column('model_sha256', sa.String(64), nullable=False),
        sa.Column('classification', sa.String(32), nullable=False),
        sa.Column('speech_score', sa.Float(), nullable=False), sa.Column('music_score', sa.Float(), nullable=False), sa.Column('singing_score', sa.Float(), nullable=False),
        sa.Column('speech_patch_ratio', sa.Float(), nullable=False), sa.Column('music_patch_ratio', sa.Float(), nullable=False), sa.Column('singing_patch_ratio', sa.Float(), nullable=False),
        sa.Column('top_classes', sa.JSON().with_variant(postgresql.JSONB, 'postgresql')), sa.Column('processing_ms', sa.Integer()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('video_id', 'classifier', 'classifier_version', name='uq_audio_assessment_video_classifier_version'),
        sa.CheckConstraint("classification IN ('speech','music','singing','mixed','ambiguous')", name='ck_audio_assessment_classification'),
        sa.CheckConstraint('speech_score >= 0 AND speech_score <= 1 AND music_score >= 0 AND music_score <= 1 AND singing_score >= 0 AND singing_score <= 1 AND speech_patch_ratio >= 0 AND speech_patch_ratio <= 1 AND music_patch_ratio >= 0 AND music_patch_ratio <= 1 AND singing_patch_ratio >= 0 AND singing_patch_ratio <= 1', name='ck_audio_assessment_scores'))
    op.create_index('ix_audio_assessments_video_id', 'audio_assessments', ['video_id'])
    if op.get_bind().dialect.name == 'sqlite':
        with op.batch_alter_table('transcription_jobs') as batch:
            batch.drop_constraint('ck_job_status', type_='check')
            batch.create_check_constraint('ck_job_status', JOB_STATUS)
            batch.add_column(sa.Column('assessment_id', sa.Uuid(), sa.ForeignKey('audio_assessments.id', name='fk_transcription_jobs_assessment_id')))
            batch.add_column(sa.Column('classifier_error_code', sa.String(64)))
            batch.add_column(sa.Column('skip_reason', sa.String(32)))
        return
    op.drop_constraint('ck_job_status', 'transcription_jobs', type_='check')
    op.create_check_constraint('ck_job_status', 'transcription_jobs', JOB_STATUS)
    op.add_column('transcription_jobs', sa.Column('assessment_id', sa.Uuid(), sa.ForeignKey('audio_assessments.id', name='fk_transcription_jobs_assessment_id')))
    op.add_column('transcription_jobs', sa.Column('classifier_error_code', sa.String(64)))
    op.add_column('transcription_jobs', sa.Column('skip_reason', sa.String(32)))


def downgrade():
    if op.get_bind().dialect.name == 'sqlite':
        with op.batch_alter_table('transcription_jobs') as batch:
            batch.drop_column('skip_reason')
            batch.drop_column('assessment_id')
            batch.drop_column('classifier_error_code')
            batch.drop_constraint('ck_job_status', type_='check')
            batch.create_check_constraint('ck_job_status', OLD_STATUS)
    else:
        op.drop_column('transcription_jobs', 'skip_reason')
        op.drop_column('transcription_jobs', 'assessment_id')
        op.drop_column('transcription_jobs', 'classifier_error_code')
        op.drop_constraint('ck_job_status', 'transcription_jobs', type_='check')
        op.create_check_constraint('ck_job_status', OLD_STATUS)
    op.drop_index('ix_audio_assessments_video_id', table_name='audio_assessments')
    op.drop_table('audio_assessments')
