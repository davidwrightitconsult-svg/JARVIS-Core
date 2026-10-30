"""Version endpoint — the official identity of the platform."""

from fastapi import APIRouter

router = APIRouter(tags=["system"])


@router.get("/api/version")
def get_version() -> dict:
    """Return the platform name, acronym, and API version."""
    return {
        "name": "A.L.F.R.E.D.",
        "acronym": "Autonomous Logic & Financial Risk Evaluation Director",
        "version": "0.1.0",
        "phase": "10",
    }
