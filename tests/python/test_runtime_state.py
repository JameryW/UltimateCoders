"""Runtime record serialization across actual concurrent database sessions."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from ultimate_coders.runtime_state import RuntimeState


def exercise_records(factory):
    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(lambda _: factory(), range(8)))
        list(
            pool.map(
                lambda index: stores[index % 8].mutate(
                    "fixture", "counter", lambda old: {"count": old.get("count", 0) + 1}
                ),
                range(80),
            )
        )
    assert stores[0].get("fixture", "counter") == {"count": 80}
    assert stores[1].records("fixture") == [{"key": "counter", "count": 80}]


def test_sqlite_concurrent_records(tmp_path):
    exercise_records(lambda: RuntimeState(tmp_path / "state.db", database_url=""))


@pytest.mark.integration
def test_postgres_cold_start_and_concurrent_records():
    url = os.environ.get("UC_RUNTIME_TEST_PG_URL")
    if not url:
        pytest.skip("Set UC_RUNTIME_TEST_PG_URL to test an isolated PostgreSQL database")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    name = "uc_reliability_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        options = conninfo_to_dict(url)
        options["dbname"] = name
        from psycopg.conninfo import make_conninfo

        isolated = make_conninfo(**options)
        exercise_records(lambda: RuntimeState(database_url=isolated))
    finally:
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
