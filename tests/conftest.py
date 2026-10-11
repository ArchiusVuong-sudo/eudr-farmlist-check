"""Shared fixtures: one in-memory PGlite PostGIS server for the whole session, replay mode, no network."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

if os.environ.get("LIVE") != "1":
    os.environ["DEMO_REPLAY"] = "1"

from farmcheck.app import create_app  # noqa: E402
from farmcheck.db import Database  # noqa: E402
from farmcheck.mapping import providers  # noqa: E402
from farmcheck.mapping.providers import ReplayProvider  # noqa: E402
from farmcheck.services import Engine  # noqa: E402

VN_MAPPING = {
    "STT": "row_ref", "Mã thửa": "plot_code", "Tên nông hộ": "producer", "Xã": "admin_area", "Huyện": "municipality",
    "Tỉnh": "province", "Vĩ độ": "latitude", "Kinh độ": "longitude", "Diện tích (ha)": "area_ha",
    "Ranh giới thửa (WKT)": "boundary_wkt", "Ghi chú": "notes",
}


@pytest.fixture(scope="session")
def db() -> Iterator[Database]:
    providers.live_available.cache_clear()
    database = Database(None)
    database.start()
    try:
        yield database
    finally:
        database.stop()


@pytest.fixture(scope="session")
def engine(db: Database) -> Engine:
    return Engine(db)


@pytest.fixture(scope="session")
def app(engine: Engine):
    application = create_app(engine, provider_factory=ReplayProvider)
    application.config["TESTING"] = True
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def conn(db: Database):
    with db.session() as c:
        yield c
