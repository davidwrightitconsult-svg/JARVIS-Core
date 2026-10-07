"""JARVIS API version endpoint.

Phase 8a: the smallest possible router, used to validate the router
folder structure works with the existing app.main FastAPI instance.
"""

from fastapi import APIRouter

router = APIRouter(tags=["system"])


@router.get("/api/version")
def get_version() -> dict:
    """Return the JARVIS platform name and API version.

    Used by the frontend to confirm it is talking to a JARVIS-compatible
    backend, and by health checks to fingerprint the deployment.
    """
    return {
        "name": "JARVIS",
        "version": "0.1.0",
        "phase": "8a",
    }
