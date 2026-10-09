import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from app.agent.providers import llm_from_env
from app.agent.workflow import recover_interrupted_runs
from app.api.approvals import router as approvals_router
from app.api.deps import DemoActor, LLMFactory
from app.api.errors import install_error_handlers
from app.api.runs import router as runs_router
from app.api.system import router as system_router
from app.api.vendors import router as vendors_router
from app.persistence.database import create_db_engine, create_session_factory, init_db

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
DEFAULT_DEMO_ACTOR = "demo.compliance.officer"


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    llm_factory: LLMFactory | None = None,
    demo_actor: str | None = None,
) -> FastAPI:
    """Wire the app. Tests inject a session factory and a fake LLM; production reads the
    environment (DATABASE_URL, LLM_PROVIDER and that provider's key/model, DEMO_ACTOR).
    Nothing connects at import."""
    factory = session_factory or create_session_factory(create_db_engine())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        init_db(factory.kw["bind"])  # idempotent: creates tables, applies additive upgrades
        recover_interrupted_runs(factory)  # runs in flight when a previous process died
        yield

    app = FastAPI(title="ComplianceOps", lifespan=lifespan)
    app.state.session_factory = factory
    app.state.llm_factory = llm_factory or llm_from_env
    app.state.demo_actor = DemoActor(
        name=demo_actor or os.environ.get("DEMO_ACTOR") or DEFAULT_DEMO_ACTOR
    )
    install_error_handlers(app)
    for router in (system_router, runs_router, vendors_router, approvals_router):
        app.include_router(router)

    if FRONTEND_DIR.is_dir():
        app.mount("/app", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

        @app.get("/", include_in_schema=False)
        def root() -> RedirectResponse:
            return RedirectResponse("/app/")

    return app


app = create_app()
