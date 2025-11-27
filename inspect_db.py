#!/usr/bin/env python3
"""
Simple SQLite database explorer.

Usage:
    python inspect_db.py [path_to_db]

If no path is given, defaults to "mock_game2.db".
"""

import sqlite3
import sys
from pathlib import Path
from typing import List, Tuple


def get_connection(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database file not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row  # so we can access columns by name
    return conn


def list_tables(conn: sqlite3.Connection) -> List[str]:
    """
    Return a list of user tables (excluding internal sqlite_* tables).
    """
    cur = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name;
        """
    )
    return [row["name"] for row in cur.fetchall()]


def get_table_info(conn: sqlite3.Connection, table_name: str) -> List[Tuple[int, str, str, int, str, int]]:
    """
    Return PRAGMA table_info for the given table.
    Each row: (cid, name, type, notnull, dflt_value, pk)
    """
    cur = conn.execute(f"PRAGMA table_info({table_name});")
    return cur.fetchall()


def get_row_count(conn: sqlite3.Connection, table_name: str) -> int:
    cur = conn.execute(f"SELECT COUNT(*) AS cnt FROM {table_name};")
    row = cur.fetchone()
    return row["cnt"] if row is not None else 0


def get_sample_row(conn: sqlite3.Connection, table_name: str):
    """
    Return a single sample row from the table (or None if empty).
    """
    cur = conn.execute(f"SELECT * FROM {table_name} LIMIT 1;")
    row = cur.fetchone()
    return row  # this is sqlite3.Row or None


def print_table_summary(conn: sqlite3.Connection, table_name: str) -> None:
    print("=" * 80)
    print(f"Table: {table_name}")
    print("-" * 80)

    # Columns
    info = get_table_info(conn, table_name)
    print("Columns:")
    for cid, name, col_type, notnull, dflt_value, pk in info:
        pk_str = " (PK)" if pk else ""
        nn_str = " NOT NULL" if notnull else ""
        default_str = f" DEFAULT {dflt_value}" if dflt_value is not None else ""
        print(f"  - {name}: {col_type}{pk_str}{nn_str}{default_str}")
    print()

    # Row count
    count = get_row_count(conn, table_name)
    print(f"Row count: {count}")
    print()

    # Sample row
    sample = get_sample_row(conn, table_name)
    if sample is None:
        print("Sample row: (table is empty)")
    else:
        print("Sample row:")
        # sample is sqlite3.Row, behaves like dict
        for col in sample.keys():
            print(f"  {col}: {sample[col]}")
    print()


def main():
    # Determine DB path
    if len(sys.argv) > 1:
        db_path = Path(sys.argv[1])
    else:
        db_path = Path("mock_game2.db")

    print(f"Inspecting database: {db_path}")
    conn = get_connection(db_path)

    tables = list_tables(conn)
    if not tables:
        print("No user tables found.")
        conn.close()
        return

    print(f"Found {len(tables)} table(s): {', '.join(tables)}\n")

    for t in tables:
        print_table_summary(conn, t)

    conn.close()


if __name__ == "__main__":
    main()
