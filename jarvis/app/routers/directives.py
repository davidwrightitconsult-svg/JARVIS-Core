"""Directives REST API — the control plane for the Directive Command Center."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from jarvis.directives import (
    get_sentinel,
    get_arbiter,
    get_courier,
    get_terminator,
)

router = APIRouter(prefix="/api/directives", tags=["directives"])


# ---- Models ----

class DirectiveRequest(BaseModel):
    text: str


class DirectiveApplyRequest(BaseModel):
    key: str
    value: object


class ResumeRequest(BaseModel):
    confirm: str = ""


# ---- Endpoints ----

@router.get("/state")
def get_directive_state() -> dict:
    sentinel = get_sentinel()
    arbiter = get_arbiter()
    terminator = get_terminator()
    return {
        "rules": sentinel.get_all(),
        "streak": arbiter.snapshot(),
        "terminated": terminator.snapshot(),
    }


@router.post("/directive")
def set_directive(req: DirectiveRequest) -> dict:
    """Parse a natural-language directive and apply it."""
    sentinel = get_sentinel()
    result = sentinel.parse_and_apply(req.text)
    return result


@router.post("/set")
def set_rule(req: DirectiveApplyRequest) -> dict:
    """Directly set a specific rule key/value."""
    sentinel = get_sentinel()
    sentinel.set(req.key, req.value)
    return {"applied": True, "key": req.key, "value": req.value}


@router.post("/resume")
def resume_platform(req: ResumeRequest) -> dict:
    """Resume trading after termination. Requires confirmation."""
    if req.confirm != "RESUME":
        raise HTTPException(status_code=400, detail="must confirm with 'RESUME'")
    terminator = get_terminator()
    arbiter = get_arbiter()
    sentinel = get_sentinel()

    terminator.resume(by="user")
    arbiter.reset_streak()
    sentinel.disable_survival_mode()

    return {
        "resumed": True,
        "reason": "resumed by user confirmation",
    }


@router.get("/leaderboard")
def get_leaderboard(window: str = "all", limit: int = 10) -> dict:
    """Fetch top traders (COURIER)."""
    courier = get_courier()
    traders = courier.fetch_top_traders(limit=limit, window=window)
    return {
        "window": window,
        "count": len(traders),
        "traders": [
            {
                "rank": t.rank,
                "address": t.address,
                "username": t.username,
                "volume_usd": t.volume_usd,
                "pnl_usd": t.pnl_usd,
                "roi": t.roi,
            }
            for t in traders
        ],
    }


@router.post("/record-trade")
def record_trade(result: str) -> dict:
    """Called by EXECUTOR after each trade to update ARBITER."""
    if result not in ("win", "loss"):
        raise HTTPException(status_code=400, detail="result must be 'win' or 'loss'")

    arbiter = get_arbiter()
    sentinel = get_sentinel()
    terminator = get_terminator()

    state = arbiter.record_win() if result == "win" else arbiter.record_loss()
    streak = state["consecutive_losses"]

    terminated = False
    if sentinel.is_survival_mode() and arbiter.should_terminate(
        sentinel.max_consecutive_losses()
    ):
        terminator.terminate(
            reason=f"{streak} consecutive losses reached survival limit of {sentinel.max_consecutive_losses()}"
        )
        terminated = True

    return {
        "recorded": result,
        "streak": state,
        "terminated": terminated,
    }
