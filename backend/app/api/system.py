"""Liveness, readiness and non-secret runtime metadata for the UI."""

import logging
import os
from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import text

from app.agent.llm import LLMError
from app.api.deps import DemoActorDep, LLMFactoryDep, SessionFactoryDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


@router.get("/health", summary="Liveness: the process is up")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", summary="Readiness: database reachable; reports LLM configuration")
def ready(
    response: Response, factory: SessionFactoryDep, make_llm: LLMFactoryDep
) -> dict[str, Any]:
    try:
        with factory() as session:
            session.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        logger.exception("readiness check: database unavailable")
        database = "unavailable"
        response.status_code = 503
    try:
        make_llm()
        llm_configured = True
    except LLMError:
        llm_configured = False
    return {
        "status": "ok" if database == "ok" else "unavailable",
        "database": database,
        "llm_configured": llm_configured,  # whether runs can start; never the key itself
    }


@router.get("/meta", summary="Non-secret deployment info for the UI")
def meta(actor: DemoActorDep) -> dict[str, Any]:
    return {
        "llm_provider": os.environ.get("LLM_PROVIDER", "anthropic").strip().lower(),
        "demo_actor": actor.name,
        "demo_actor_is_authentication": False,
        "note": "Approvals are recorded as this fixed demo actor. There is no authentication.",
    }
