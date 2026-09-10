import asyncio
import logging
from sqlalchemy.orm import Session
from .config import get_settings
from .db import get_engine
from .jobs import cleanup_audio, requeue_pending_jobs


def reconcile_once():
    with Session(get_engine()) as db:
        requeue_pending_jobs(db)
        cleanup_audio(db)


async def maintenance_loop():
    while True:
        await asyncio.sleep(get_settings().reconciliation_interval_seconds)
        try:
            await asyncio.to_thread(reconcile_once)
        except Exception:
            logging.getLogger('kurukin.maintenance').warning('Reconciliation unavailable; retry next interval')


if __name__ == '__main__':
    reconcile_once()
