from alembic import context
from sqlalchemy import create_engine, pool
from app.config import get_settings
from app.models import Base
from app.migration_diagnostics import safe_migration_error


def run():
    url = get_settings().resolve_database_url()
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True,
                               connect_args={'connect_timeout': 5})
        try:
            with engine.connect() as connection:
                context.configure(connection=connection, target_metadata=Base.metadata)
                with context.begin_transaction():
                    context.run_migrations()
        finally:
            engine.dispose()

try:
    run()
except Exception as exc:
    raise SystemExit(safe_migration_error(exc)) from None
