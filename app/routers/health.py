from fastapi import APIRouter

router = APIRouter()

@router.get("/healthz", summary="Liveness Probe")
async def healthz():
    """Checks if the service is running."""
    return {"status": "ok"}

@router.get("/readyz", summary="Readiness Probe")
async def readyz():
    """Checks if the service is ready to accept traffic."""
    # In a real app, this would check dependencies like model loading status.
    return {"ready": True}
