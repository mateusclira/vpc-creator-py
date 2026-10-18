import json
import os
import sqlite3
from typing import Optional

DB_PATH = os.getenv("DATABASE_PATH", "vpcs.db")


def init_db(path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vpcs (
                id         TEXT PRIMARY KEY,
                aws_vpc_id TEXT NOT NULL,
                cidr       TEXT NOT NULL,
                region     TEXT NOT NULL,
                subnets    TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stats (
                key   TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        for key in ("vpcs_created", "vpcs_deleted"):
            conn.execute(
                "INSERT OR IGNORE INTO stats (key, value) VALUES (?, 0)", (key,)
            )


def get_stat(key: str, path: str = DB_PATH) -> int:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT value FROM stats WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else 0


def increment_stat(key: str, path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO stats (key, value) VALUES (?, 1)
            ON CONFLICT(key) DO UPDATE SET value = value + 1
            """,
            (key,),
        )


def save_vpc(vpc: dict, path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO vpcs (id, aws_vpc_id, cidr, region, subnets, created_at) VALUES (?,?,?,?,?,?)",
            (
                vpc["id"],
                vpc["aws_vpc_id"],
                vpc["cidr"],
                vpc["region"],
                json.dumps(vpc["subnets"]),
                vpc["created_at"],
            ),
        )


def get_vpc(vpc_id: str, path: str = DB_PATH) -> Optional[dict]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM vpcs WHERE id = ?", (vpc_id,)).fetchone()
    return _to_dict(row) if row else None


def list_vpcs(path: str = DB_PATH) -> list:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM vpcs ORDER BY created_at DESC"
        ).fetchall()
    return [_to_dict(r) for r in rows]


def delete_vpc(vpc_id: str, path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM vpcs WHERE id = ?", (vpc_id,))


def _to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "aws_vpc_id": row["aws_vpc_id"],
        "cidr": row["cidr"],
        "region": row["region"],
        "subnets": json.loads(row["subnets"]),
        "created_at": row["created_at"],
    }
