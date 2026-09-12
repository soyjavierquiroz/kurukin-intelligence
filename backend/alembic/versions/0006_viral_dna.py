"""Versioned global deterministic Viral DNA features."""
from alembic import op
import sqlalchemy as sa


revision = '0006_viral_dna'
down_revision = '0005_global_incremental_corpus'
branch_labels = None
depends_on = None


SEMANTIC_STATUS = "semantic_status IN ('not_requested','pending','completed','skipped_no_transcript','failed')"


def upgrade():
    op.create_table(
        'viral_dna',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('video_id', sa.Uuid(), nullable=False),
        sa.Column('extractor_version', sa.String(length=64), nullable=False),
        sa.Column('deterministic_input_sha256', sa.String(length=64), nullable=False),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('caption_present', sa.Boolean(), nullable=False),
        sa.Column('caption_char_count', sa.Integer(), nullable=False),
        sa.Column('transcript_id', sa.Uuid(), nullable=True),
        sa.Column('transcript_word_count', sa.Integer(), nullable=True),
        sa.Column('transcript_duration_seconds', sa.Float(), nullable=True),
        sa.Column('words_per_second', sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column('audio_assessment_id', sa.Uuid(), nullable=True),
        sa.Column('semantic_status', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(SEMANTIC_STATUS, name='ck_viral_dna_semantic_status'),
        sa.ForeignKeyConstraint(['audio_assessment_id'], ['audio_assessments.id']),
        sa.ForeignKeyConstraint(['transcript_id'], ['transcripts.id']),
        sa.ForeignKeyConstraint(['video_id'], ['videos.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('video_id', 'extractor_version', name='uq_viral_dna_video_extractor_version'),
    )
    # The unique constraint supplies an index beginning with video_id.
    op.create_index('ix_viral_dna_audio_assessment_id', 'viral_dna', ['audio_assessment_id'])


def downgrade():
    op.drop_index('ix_viral_dna_audio_assessment_id', table_name='viral_dna')
    op.drop_table('viral_dna')
