from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nff_diagnosis.auth.router import router as auth_router
from nff_diagnosis.auth.store import AgentStore
from nff_diagnosis.engine.diagnosis import DiagnosisEngine
from nff_diagnosis.server.routes import router
from nff_diagnosis.storage.sqlite import SQLiteStorage
from nff_diagnosis.store.local import LocalELFStore


def create_app(
    store_root: Path,
    db_path: Path,
    api_key: str | None = None,
    model: str = "claude-sonnet-4-6",
) -> FastAPI:
    app = FastAPI(title="nff-diagnosis", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.store = LocalELFStore(store_root)
    app.state.storage = SQLiteStorage(db_path)
    app.state.engine = DiagnosisEngine(api_key=api_key, model=model)
    app.state.agent_store = AgentStore(db_path)
    app.include_router(router)
    app.include_router(auth_router)
    return app
