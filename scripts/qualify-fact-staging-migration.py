#!/usr/bin/env python3
"""Qualify crash-safe normalized fact staging migration with synthetic SQLite."""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def base(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        create table records(kind text, key text, sequence integer, payload text);
        insert into records values('graph_publication','visible',1,'{"deleted":false}');
        create table staged_fact_rows(
          agent_id text, graph_id text, fact_set_digest text, batch_index integer,
          batch_count integer, row_count integer, rows_json text,
          primary key(agent_id,graph_id,batch_index));
        insert into staged_fact_rows values('agent','graph','digest',0,1,1,'[{}]');
        """
    )


def create_normalized(connection: sqlite3.Connection) -> None:
    for statement in (
        "create table if not exists fact_schema(id integer primary key check(id=1), version integer not null)",
        """create table if not exists staged_fact_items(
          agent_id text, graph_id text, fact_set_digest text, batch_index integer,
          kind text, fact_id text, parent_id text, order_index integer,
          row_hash text, payload text)""",
        """create table if not exists staged_fact_generations(
          agent_id text, graph_id text, generation integer)""",
        """create table if not exists validated_fact_graphs(
          agent_id text, graph_id text, fact_set_digest text, generation integer,
          fact_count integer, batch_count integer, encoded_bytes integer,
          manifest text)""",
    ):
        connection.execute(statement)


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute("begin")
    create_normalized(connection)
    version = connection.execute(
        "select version from fact_schema where id=1"
    ).fetchone()
    if version is None:
        for table in (
            "staged_fact_rows",
            "staged_fact_items",
            "staged_fact_generations",
            "validated_fact_graphs",
        ):
            connection.execute(f"delete from {table}")
        connection.execute(
            """insert into fact_schema values(1,1)
              on conflict(id) do update set version=excluded.version"""
        )
    connection.commit()


def missing_batches(connection: sqlite3.Connection) -> list[int]:
    present = {
        row[0]
        for row in connection.execute(
            """
            select batch.batch_index from staged_fact_rows batch
            where batch.agent_id='agent' and batch.graph_id='graph'
              and batch.fact_set_digest='digest'
              and batch.row_count=(select count(*) from staged_fact_items item
                where item.agent_id=batch.agent_id
                  and item.graph_id=batch.graph_id
                  and item.fact_set_digest=batch.fact_set_digest
                  and item.batch_index=batch.batch_index)
            """
        )
    }
    return [index for index in range(1) if index not in present]


def assert_visible(connection: sqlite3.Connection) -> None:
    assert connection.execute("select count(*) from records").fetchone()[0] == 1


def scenario(phase: str) -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    base(connection)
    if phase == "after_tables":
        create_normalized(connection)
        assert missing_batches(connection) == [0]
    elif phase == "after_cleanup":
        create_normalized(connection)
        connection.execute("delete from staged_fact_rows")
    elif phase == "after_marker":
        migrate(connection)
    elif phase == "rolled_back_transaction":
        connection.execute("begin")
        create_normalized(connection)
        connection.execute("delete from staged_fact_rows")
        connection.rollback()
    else:
        raise AssertionError(phase)
    migrate(connection)
    assert connection.execute("select version from fact_schema").fetchone()[0] == 1
    assert (
        connection.execute("select count(*) from staged_fact_rows").fetchone()[0] == 0
    )
    assert_visible(connection)
    connection.close()


def main() -> None:
    source = (ROOT / "cloudflare/control-plane/src/workspace.ts").read_text()
    facts = (ROOT / "cloudflare/control-plane/src/facts.ts").read_text()
    assert "transactionSync(() => initializeFacts" in source
    assert "CREATE TABLE IF NOT EXISTS fact_schema" in facts
    assert "INSERT INTO fact_schema VALUES(1,1)" in facts
    assert "batch.row_count=(SELECT count(*) FROM staged_fact_items" in facts
    for phase in (
        "after_tables",
        "after_cleanup",
        "after_marker",
        "rolled_back_transaction",
    ):
        scenario(phase)
    print(
        "fact staging migration: PASS (4 interruption phases, visible state preserved)"
    )


if __name__ == "__main__":
    main()
