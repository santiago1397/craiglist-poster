from __future__ import annotations

from fastapi import APIRouter, Depends

from ..db import tx
from ..schemas.events import EventBatch, EventEnvelope
from ..security import require_ingest_token
from ..services.ingest import ingest_events

router = APIRouter(dependencies=[Depends(require_ingest_token)])


@router.post("")
def ingest_one(envelope: EventEnvelope) -> dict:
    """Ingest a single event. Idempotent by event_id."""
    with tx() as conn:
        summary = ingest_events(conn, [envelope.event])
    return summary


@router.post("/batch")
def ingest_batch(batch: EventBatch) -> dict:
    """Ingest up to 500 events in one transaction."""
    with tx() as conn:
        summary = ingest_events(conn, batch.events)
    return summary
