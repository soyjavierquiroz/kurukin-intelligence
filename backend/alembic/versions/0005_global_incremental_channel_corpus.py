"""Global incremental channel identity, observations, and acquisitions.

The additions are nullable/defaulted where needed so an installed 0.3.x corpus
is retained intact.  Existing per-analysis transcription ranks are copied into
the normalized association table; no transcript, assessment, or snapshot is
discarded.
"""
from alembic import op
import sqlalchemy as sa


revision = '0005_global_incremental_corpus'
down_revision = '0004_video_music_metadata'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('channels', sa.Column('tiktok_user_id', sa.String(length=64), nullable=True))
    if op.get_bind().dialect.name == 'sqlite':
        # 0001 created the old handle constraint without a name.  The naming
        # convention makes that reflected SQLite constraint addressable.
        with op.batch_alter_table('channels', naming_convention={
                'uq': 'uq_%(table_name)s_%(column_0_name)s'}) as batch:
            batch.drop_constraint('uq_channels_platform', type_='unique')
            batch.create_unique_constraint('uq_channels_platform_tiktok_user_id',
                                           ['platform', 'tiktok_user_id'])
    else:
        op.drop_constraint('channels_platform_username_key', 'channels', type_='unique')
        op.create_unique_constraint('uq_channels_platform_tiktok_user_id', 'channels',
                                    ['platform', 'tiktok_user_id'])

    op.add_column('videos', sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('videos', sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True))
    op.execute('UPDATE videos SET first_seen_at = created_at WHERE first_seen_at IS NULL')
    op.execute('UPDATE videos SET last_seen_at = updated_at WHERE last_seen_at IS NULL')
    if op.get_bind().dialect.name == 'sqlite':
        with op.batch_alter_table('videos') as batch:
            batch.alter_column('first_seen_at', nullable=False)
            batch.alter_column('last_seen_at', nullable=False)
    else:
        op.alter_column('videos', 'first_seen_at', nullable=False)
        op.alter_column('videos', 'last_seen_at', nullable=False)

    op.add_column('analyses', sa.Column('videos_new', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('analyses', sa.Column('videos_refreshed', sa.Integer(), nullable=False, server_default='0'))
    if op.get_bind().dialect.name != 'sqlite':
        op.alter_column('analyses', 'videos_new', server_default=None)
        op.alter_column('analyses', 'videos_refreshed', server_default=None)

    op.create_table('analysis_acquisitions',
        sa.Column('analysis_id', sa.Uuid(), nullable=False),
        sa.Column('video_id', sa.Uuid(), nullable=False),
        sa.Column('transcription_rank', sa.Integer(), nullable=False),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['analysis_id'], ['analyses.id']),
        sa.ForeignKeyConstraint(['video_id'], ['videos.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('analysis_id', 'video_id', name='uq_analysis_acquisition_video'))
    op.create_index(op.f('ix_analysis_acquisitions_analysis_id'), 'analysis_acquisitions', ['analysis_id'], unique=False)
    op.create_index(op.f('ix_analysis_acquisitions_video_id'), 'analysis_acquisitions', ['video_id'], unique=False)
    # Deterministic UUIDs make this backfill independent from optional PostgreSQL
    # extensions such as pgcrypto.
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("""INSERT INTO analysis_acquisitions (id, created_at, analysis_id, video_id, transcription_rank)
                      SELECT (substr(md5(analysis_id::text || video_id::text), 1, 8) || '-' ||
                              substr(md5(analysis_id::text || video_id::text), 9, 4) || '-' ||
                              substr(md5(analysis_id::text || video_id::text), 13, 4) || '-' ||
                              substr(md5(analysis_id::text || video_id::text), 17, 4) || '-' ||
                              substr(md5(analysis_id::text || video_id::text), 21, 12))::uuid,
                             created_at, analysis_id, video_id, transcription_rank
                      FROM video_snapshots WHERE transcription_rank IS NOT NULL""")
    elif op.get_bind().dialect.name == 'sqlite':
        op.execute("""INSERT INTO analysis_acquisitions (id, created_at, analysis_id, video_id, transcription_rank)
                      SELECT lower(hex(randomblob(16))), created_at, analysis_id, video_id, transcription_rank
                      FROM video_snapshots WHERE transcription_rank IS NOT NULL""")


def downgrade():
    op.drop_index(op.f('ix_analysis_acquisitions_video_id'), table_name='analysis_acquisitions')
    op.drop_index(op.f('ix_analysis_acquisitions_analysis_id'), table_name='analysis_acquisitions')
    op.drop_table('analysis_acquisitions')
    op.drop_column('analyses', 'videos_refreshed')
    op.drop_column('analyses', 'videos_new')
    op.drop_column('videos', 'last_seen_at')
    op.drop_column('videos', 'first_seen_at')
    if op.get_bind().dialect.name == 'sqlite':
        with op.batch_alter_table('channels') as batch:
            batch.drop_constraint('uq_channels_platform_tiktok_user_id', type_='unique')
            batch.create_unique_constraint('uq_channels_platform_username', ['platform', 'username'])
    else:
        op.drop_constraint('uq_channels_platform_tiktok_user_id', 'channels', type_='unique')
        op.create_unique_constraint('channels_platform_username_key', 'channels', ['platform', 'username'])
    op.drop_column('channels', 'tiktok_user_id')
