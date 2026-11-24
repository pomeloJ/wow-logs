import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "tests"))

TOKEN = "test-write-token"
READ_TOKEN = "test-read-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def make_client(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app

    def _make(db_name="server.db"):
        return TestClient(create_app(str(tmp_path / db_name), TOKEN, READ_TOKEN))
    return _make


@pytest.fixture
def client(make_client):
    return make_client()
