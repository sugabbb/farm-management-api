import os
from urllib.parse import urlparse

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/pondok_berkah_test")
os.environ.setdefault("API_KEY", "test-api-key-with-at-least-24-characters")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-with-at-least-thirty-two-characters")


def pytest_sessionstart(session: pytest.Session) -> None:
    database_url = os.environ["DATABASE_URL"]
    database_name = urlparse(database_url.replace("postgresql+psycopg", "postgresql", 1)).path.rsplit("/", 1)[-1]
    if not database_name.endswith("_test"):
        raise pytest.UsageError("DATABASE_URL must target a database whose name ends in _test")
