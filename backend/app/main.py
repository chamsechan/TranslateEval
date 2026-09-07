from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles

from .api import router
from .import_api import router as import_router
from .config import settings
from .database import SessionLocal, init_db
from .seed import seed_defaults


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            if path.startswith("api/") or Path(path).suffix:
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404:
            if path.startswith("api/") or Path(path).suffix:
                return response
            return await super().get_response("index.html", scope)
        return response


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.import_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    with SessionLocal() as session:
        seed_defaults(session)
    yield


app = FastAPI(
    title="译研评测台 API",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(import_router)

if settings.frontend_dist.is_dir():
    app.mount("/", SPAStaticFiles(directory=settings.frontend_dist, html=True), name="frontend")
