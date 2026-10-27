import pytest
from fastapi.testclient import TestClient

from app.core.config import Config
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Config(watched_folders=[tmp_path]))) as client:
        yield client
