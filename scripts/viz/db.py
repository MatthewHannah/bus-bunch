"""Shared SQL connection helper.

Uses DefaultAzureCredential (which cascades to the Azure CLI token from
`az login`) to fetch an access token for the Azure SQL resource, and hands it
to pyodbc via the SQL_COPT_SS_ACCESS_TOKEN connection attribute. This avoids
having to configure ODBC.ini or store any passwords.
"""
from __future__ import annotations

import os
import struct
import warnings
from functools import lru_cache

import pandas as pd
import pyodbc
from azure.identity import DefaultAzureCredential

# pandas nags about non-SQLAlchemy DBAPI connections; pyodbc works fine here.
warnings.filterwarnings(
    "ignore",
    message="pandas only supports SQLAlchemy connectable.*",
    category=UserWarning,
)

SERVER = os.environ.get(
    "BUSBUNCH_SQL_SERVER",
    "busbunch-sql-dkhvkrmhbltmw.database.windows.net",
)
DATABASE = os.environ.get("BUSBUNCH_SQL_DB", "busbunch")
DRIVER = os.environ.get("BUSBUNCH_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")

_SQL_COPT_SS_ACCESS_TOKEN = 1256


@lru_cache(maxsize=1)
def _credential() -> DefaultAzureCredential:
    return DefaultAzureCredential(exclude_interactive_browser_credential=False)


def _access_token_struct() -> bytes:
    token = _credential().get_token("https://database.windows.net/.default").token
    raw = token.encode("utf-16-le")
    return struct.pack(f"<I{len(raw)}s", len(raw), raw)


def get_conn() -> pyodbc.Connection:
    conn_str = (
        f"Driver={{{DRIVER}}};"
        f"Server=tcp:{SERVER},1433;"
        f"Database={DATABASE};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    return pyodbc.connect(
        conn_str,
        attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: _access_token_struct()},
    )


def query(sql: str, params: tuple | list | None = None) -> pd.DataFrame:
    with get_conn() as cn:
        return pd.read_sql(sql, cn, params=params)
