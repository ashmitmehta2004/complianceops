"""FastAPI dependencies shared by the routers. Everything comes from `app.state` so tests can
inject their own session factory, LLM factory and demo actor."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from app.agent.llm import LLMClient

LLMFactory = Callable[[], LLMClient]


@dataclass(frozen=True)
class DemoActor:
    """The acting human for approval decisions in the local demo.

    This is a server-side setting, NOT authentication: any caller who can reach the API acts as
    this identity. The client cannot choose its own name or role."""

    name: str


def _session_factory(request: Request) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    return factory


def _llm_factory(request: Request) -> LLMFactory:
    factory: LLMFactory = request.app.state.llm_factory
    return factory


def _demo_actor(request: Request) -> DemoActor:
    actor: DemoActor = request.app.state.demo_actor
    return actor


SessionFactoryDep = Annotated[sessionmaker[Session], Depends(_session_factory)]
LLMFactoryDep = Annotated[LLMFactory, Depends(_llm_factory)]
DemoActorDep = Annotated[DemoActor, Depends(_demo_actor)]
