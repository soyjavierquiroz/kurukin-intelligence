"""Add nullable global TikTok sound metadata to videos.

Lengths are intentionally bounded to public TikTok values: music ID 64 chars,
title 512 chars, author 256 chars.  The fields belong to the global video row,
not an analysis-specific snapshot.
"""
from alembic import op
import sqlalchemy as sa


revision = '0004_video_music_metadata'
down_revision = '0003_audio_assessment'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('videos', sa.Column('music_id', sa.String(length=64), nullable=True))
    op.add_column('videos', sa.Column('music_title', sa.String(length=512), nullable=True))
    op.add_column('videos', sa.Column('music_author', sa.String(length=256), nullable=True))
    op.add_column('videos', sa.Column('music_original', sa.Boolean(), nullable=True))


def downgrade():
    op.drop_column('videos', 'music_original')
    op.drop_column('videos', 'music_author')
    op.drop_column('videos', 'music_title')
    op.drop_column('videos', 'music_id')
