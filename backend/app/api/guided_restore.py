"""Guided database restore API: load one database back into its container from a snapshot.

POST /api/restore/database/preview   check everything, change nothing
POST /api/restore/database           start (typed confirmation required), 202
GET  /api/restore/database           history
GET  /api/restore/database/options   discovered databases and how each restores
GET  /api/restore/database/{id}      one operation with its step log
POST /api/restore/database/{id}/undo load the safety dump taken before it, 202
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.dependencies import get_config, require_auth
from app.models.guided_restore import GuidedRestoreSelection, GuidedRestoreStart, GuidedRestoreUndo
from app.services.guided_restore import GuidedRestoreEngine, GuidedRestoreError

logger = logging.getLogger("arkive.api.guided_restore")

router = APIRouter(prefix="/restore/database", tags=["restore"], dependencies=[Depends(require_auth)])


def get_guided_restore(request: Request) -> GuidedRestoreEngine:
    """The app's engine; created on first use when startup did not provide one."""
    state = request.app.state
    engine = getattr(state, "guided_restore", None)
    if engine is None:
        orchestrator = getattr(state, "orchestrator", None)
        engine = GuidedRestoreEngine(
            get_config(),
            backup_engine=getattr(state, "backup_engine", None),
            docker_client=getattr(state, "docker_client", None),
            lock_manager=getattr(orchestrator, "lock_manager", None),
        )
        state.guided_restore = engine
    return engine


def _http_error(exc: GuidedRestoreError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/options")
async def restore_options(engine: GuidedRestoreEngine = Depends(get_guided_restore)):
    """Discovered databases, whether each can be restored in place, and the apps that use it."""
    return {"databases": await engine.options()}


@router.post("/preview")
async def preview_restore(body: GuidedRestoreSelection, engine: GuidedRestoreEngine = Depends(get_guided_restore)):
    """Find the database's dump in the snapshot and check the container; nothing is changed."""
    try:
        return await engine.preview(body)
    except GuidedRestoreError as exc:
        raise _http_error(exc) from exc


@router.post("", status_code=202)
async def start_restore(body: GuidedRestoreStart, engine: GuidedRestoreEngine = Depends(get_guided_restore)):
    """Start a guided restore in the background; poll GET /restore/database/{id}."""
    try:
        return await engine.start(body)
    except GuidedRestoreError as exc:
        raise _http_error(exc) from exc


@router.get("")
async def list_restores(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    engine: GuidedRestoreEngine = Depends(get_guided_restore),
):
    """Guided restores, newest first."""
    return await engine.history(limit=limit, offset=offset)


@router.get("/{op_id}")
async def get_restore(op_id: str, engine: GuidedRestoreEngine = Depends(get_guided_restore)):
    operation = await engine.get(op_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Restore not found")
    return operation


@router.post("/{op_id}/undo", status_code=202)
async def undo_restore(op_id: str, body: GuidedRestoreUndo, engine: GuidedRestoreEngine = Depends(get_guided_restore)):
    """Load the safety dump of a previous restore back into the database."""
    try:
        return await engine.start_undo(op_id, body)
    except GuidedRestoreError as exc:
        raise _http_error(exc) from exc
