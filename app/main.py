from pathlib import Path

from fastapi import FastAPI

from app.api.routes import system
from app.core.config import Config, load_config

CONFIG_PATH = Path("config.yaml")


def create_app(config: Config | None = None) -> FastAPI:
    if config is None:
        config = load_config(CONFIG_PATH)

    app = FastAPI(title="Code Kamaji")
    app.include_router(system.router)
    return app
