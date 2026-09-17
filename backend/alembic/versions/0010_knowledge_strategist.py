"""Add reusable knowledge identities and global strategist playbooks."""
from alembic import op
import sqlalchemy as sa


revision = '0010_knowledge_strategist'
down_revision = '0009_private_content_packs'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('channel_intelligence_analyses', sa.Column('semantic_corpus_hash', sa.String(length=64), nullable=True))
    op.add_column('channel_intelligence_analyses', sa.Column('performance_state_hash', sa.String(length=64), nullable=True))
    op.add_column('channel_intelligence_analyses', sa.Column('analysis_contract_version', sa.String(length=128), nullable=True))
    op.add_column('channel_video_intelligence', sa.Column('semantic_source_hash', sa.String(length=64), nullable=True))
    op.create_index('ix_channel_intelligence_analyses_semantic_corpus_hash', 'channel_intelligence_analyses', ['semantic_corpus_hash'])
    op.create_index('ix_channel_intelligence_analyses_performance_state_hash', 'channel_intelligence_analyses', ['performance_state_hash'])
    op.create_index('ix_channel_video_intelligence_semantic_source_hash', 'channel_video_intelligence', ['semantic_source_hash'])
    op.add_column('private_content_packs', sa.Column('personal_strategy', sa.JSON(), nullable=True))
    op.create_table(
        'channel_strategic_playbooks',
        sa.Column('id', sa.Uuid(), nullable=False), sa.Column('channel_id', sa.Uuid(), nullable=False),
        sa.Column('source_analysis_id', sa.Uuid(), nullable=False), sa.Column('source_payload_sha', sa.String(length=64), nullable=False),
        sa.Column('semantic_corpus_hash', sa.String(length=64), nullable=False), sa.Column('performance_state_hash', sa.String(length=64), nullable=False),
        sa.Column('schema_version', sa.String(length=64), nullable=False), sa.Column('prompt_version', sa.String(length=64), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=False), sa.Column('model', sa.String(length=128), nullable=False),
        sa.Column('payload_sha256', sa.String(length=64), nullable=False), sa.Column('payload_json', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id']), sa.ForeignKeyConstraint(['source_analysis_id'], ['channel_intelligence_analyses.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('channel_id', 'source_payload_sha', 'performance_state_hash', 'prompt_version', 'provider', 'model', name='uq_channel_playbook_cache'),
    )
    for column in ('channel_id', 'source_analysis_id', 'source_payload_sha', 'semantic_corpus_hash', 'performance_state_hash'):
        op.create_index('ix_channel_strategic_playbooks_' + column, 'channel_strategic_playbooks', [column])
    op.create_table(
        'private_personal_strategies',
        sa.Column('id', sa.Uuid(), nullable=False), sa.Column('channel_id', sa.Uuid(), nullable=False),
        sa.Column('playbook_id', sa.Uuid(), nullable=False), sa.Column('payload_sha256', sa.String(length=64), nullable=False),
        sa.Column('private_context', sa.JSON(), nullable=False), sa.Column('strategy_json', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['channel_id'], ['channels.id']), sa.ForeignKeyConstraint(['playbook_id'], ['channel_strategic_playbooks.id']),
        sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('payload_sha256'),
    )
    op.create_index('ix_private_personal_strategies_channel_id', 'private_personal_strategies', ['channel_id'])
    op.create_index('ix_private_personal_strategies_playbook_id', 'private_personal_strategies', ['playbook_id'])


def downgrade():
    op.drop_index('ix_private_personal_strategies_playbook_id', table_name='private_personal_strategies')
    op.drop_index('ix_private_personal_strategies_channel_id', table_name='private_personal_strategies')
    op.drop_table('private_personal_strategies')
    for column in ('performance_state_hash', 'semantic_corpus_hash', 'source_payload_sha', 'source_analysis_id', 'channel_id'):
        op.drop_index('ix_channel_strategic_playbooks_' + column, table_name='channel_strategic_playbooks')
    op.drop_table('channel_strategic_playbooks')
    op.drop_column('private_content_packs', 'personal_strategy')
    op.drop_index('ix_channel_video_intelligence_semantic_source_hash', table_name='channel_video_intelligence')
    op.drop_column('channel_video_intelligence', 'semantic_source_hash')
    op.drop_index('ix_channel_intelligence_analyses_performance_state_hash', table_name='channel_intelligence_analyses')
    op.drop_index('ix_channel_intelligence_analyses_semantic_corpus_hash', table_name='channel_intelligence_analyses')
    op.drop_column('channel_intelligence_analyses', 'analysis_contract_version')
    op.drop_column('channel_intelligence_analyses', 'performance_state_hash')
    op.drop_column('channel_intelligence_analyses', 'semantic_corpus_hash')
