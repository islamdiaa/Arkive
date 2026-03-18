"""API router aggregation — all route modules mounted here."""

from fastapi import APIRouter

from app.api import (
    activity,
    auth,
    databases,
    directories,
    discover,
    events,
    jobs,
    logs,
    notifications,
    restore,
    settings,
    snapshots,
    status,
    storage,
    targets,
    verification,
)

api_router = APIRouter(prefix="/api")

api_router.include_router(status.router)
api_router.include_router(auth.router)
api_router.include_router(jobs.router)
api_router.include_router(targets.router)
api_router.include_router(snapshots.router)
api_router.include_router(restore.router)
api_router.include_router(settings.router)
api_router.include_router(logs.router)
api_router.include_router(notifications.router)
api_router.include_router(activity.router)
api_router.include_router(storage.router)
api_router.include_router(discover.router)
api_router.include_router(databases.router)
api_router.include_router(directories.router)
api_router.include_router(events.router)
api_router.include_router(verification.router)
