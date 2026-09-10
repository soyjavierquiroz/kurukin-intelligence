from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from .config import get_settings


@lru_cache
def get_engine():
    return create_engine(get_settings().resolve_database_url(), pool_pre_ping=True, hide_parameters=True,
                         connect_args={"connect_timeout": 5}, pool_timeout=5)


def get_db():
    with Session(get_engine()) as session:
        yield session
