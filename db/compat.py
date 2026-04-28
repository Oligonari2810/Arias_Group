"""
PostgreSQL ↔ SQLite Compatibility Layer for Arias Group

This module provides transparent compatibility between SQLite and PostgreSQL,
allowing app.py to work unchanged with either database backend.
"""
import json
import re
from datetime import datetime, date
from decimal import Decimal
from typing import Any, Optional


def to_db_value(value: Any, dtype: str = None) -> Any:
    """Convert Python value to database-appropriate format."""
    if value is None:
        return None
    if dtype == 'json':
        return json.dumps(value) if not isinstance(value, (str, bytes)) else value
    if dtype == 'datetime':
        return value.isoformat() if isinstance(value, (datetime, date)) else value
    return value


def from_db_value(value: Any, dtype: str = None) -> Any:
    """Convert database value to Python-appropriate format."""
    if value is None:
        return None
    if dtype == 'json':
        return value if isinstance(value, (list, dict)) else json.loads(value) if isinstance(value, str) else value
    if dtype == 'numeric' and isinstance(value, Decimal):
        return float(value)
    return value


def safe_slice_date(value: Any, end: int = 10) -> str:
    """Safely slice a date value, handling both strings and datetime objects."""
    if value is None:
        return ''
    return str(value)[:end]


def safe_json_loads(value: Any) -> Any:
    """Safely load JSON, handling both strings and already-parsed objects."""
    if value is None:
        return []
    if isinstance(value, (list, dict)):
        return value  # PostgreSQL
    if isinstance(value, str):
        return json.loads(value)  # SQLite
    return value


def translate_sql(sql: str) -> str:
    """Translate SQLite SQL to PostgreSQL SQL."""
    if not sql:
        return sql
    
    # PRAGMA statements are SQLite-only
    if 'PRAGMA' in sql.upper():
        raise NotImplementedError('PRAGMA not supported on PostgreSQL')
    
    # INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
    sql = re.sub(r'\bINSERT\s+OR\s+IGNORE\b', 'INSERT', sql, flags=re.IGNORECASE)
    if 'INSERT' in sql.upper() and 'ON CONFLICT' not in sql.upper():
        sql = sql.rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
    
    # ? placeholders → %s
    sql = sql.replace('?', '%s')
    
    # last_insert_rowid() → lastval()
    sql = sql.replace('last_insert_rowid()', 'lastval()')
    
    # SQLite boolean: is_active = 1/0 → is_active = TRUE/FALSE
    sql = re.sub(r'\bis_active\s*=\s*1\b', 'is_active = TRUE', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bis_active\s*=\s*0\b', 'is_active = FALSE', sql, flags=re.IGNORECASE)
    
    # json_extract(col, '$.path') → (col::jsonb->'path')::numeric
    def replace_json(m):
        col, path = m.group(1).strip(), m.group(2).strip()
        parts = path.split('.')
        if len(parts) == 1:
            return f"({col}::jsonb->>'{parts[0]}')::numeric"
        result = f'{col}::jsonb'
        for i, p in enumerate(parts):
            result += f"->>'{p}'" if i == len(parts) - 1 else f"->'{p}'"
        return f'({result})::numeric'
    
    sql = re.sub(r"json_extract\(([^,]+),\s*'\$\.([^']+)'\)", replace_json, sql)
    
    return sql


class CompatRow:
    """Row wrapper supporting both row['col'] and row[0] access."""
    __slots__ = ('_dict', '_keys')
    
    def __init__(self, row_dict: dict):
        self._dict = row_dict
        self._keys = list(row_dict.keys()) if row_dict else []
    
    def __getitem__(self, key):
        return self._dict[self._keys[key]] if isinstance(key, int) else self._dict[key]
    
    def __setitem__(self, key, value):
        if isinstance(key, int):
            self._dict[self._keys[key]] = value
        else:
            self._dict[key] = value
    
    def __contains__(self, key):
        return 0 <= key < len(self._keys) if isinstance(key, int) else key in self._dict
    
    def get(self, key, default=None):
        try:
            return self._dict[self._keys[key]] if isinstance(key, int) else self._dict.get(key, default)
        except (IndexError, KeyError):
            return default
    
    def __iter__(self):
        return iter(self._dict.items())
    
    def keys(self): return self._dict.keys()
    def values(self): return self._dict.values()
    def items(self): return self._dict.items()
    def __repr__(self): return repr(self._dict)
    def __len__(self): return len(self._dict)


def wrap_rows(rows: list) -> list:
    """Wrap database rows for compatibility."""
    if not rows or not isinstance(rows[0], dict):
        return rows
    return [CompatRow(r) for r in rows]


def wrap_row(row: Optional[dict]) -> Optional[dict]:
    """Wrap a single row for compatibility."""
    return CompatRow(row) if isinstance(row, dict) else row
