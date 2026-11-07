import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collections_ai import db, pipeline
from collections_ai.config import GenConfig
from collections_ai.generator import generate

N = 1500


@pytest.fixture(scope="session")
def cfg():
    return GenConfig(n_loans=N)


@pytest.fixture(scope="session")
def baseline(cfg):
    return generate(cfg, inject=False)


@pytest.fixture(scope="session")
def ds(cfg):
    return generate(cfg, inject=True)


@pytest.fixture(scope="session")
def tables(ds):
    return ds.tables


@pytest.fixture()
def api(ds, tmp_path, monkeypatch):
    """Fresh DB + TestClient per test (state-changing API tests stay isolated)."""
    from fastapi.testclient import TestClient
    p = tmp_path / "t.db"
    pipeline.build_database(ds, p)
    monkeypatch.setenv("COLLECTIONS_DB", str(p))
    from collections_ai.api.main import app
    return TestClient(app)
