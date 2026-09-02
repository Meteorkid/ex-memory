"""数据库连接抽象：同一套 SQL 跑在 SQLite 与 PostgreSQL 上。

SQLite 单文件在多副本下是错的：多个写入方共享一个文件会锁冲突甚至损坏。
FR-035 把状态搬出进程之后，数据库就成了横向扩展的最后一个阻塞点。

**迁移策略是「换引擎不换形状」**：
- 占位符 ? 自动翻成 %s，业务 SQL 一行不用改；
- datetime('now') 翻成 PG 的等价写法；
- 时间戳在两边都存 TEXT。不是不知道该用 timestamptz，而是现有代码到处
  在做字符串比较（expires_at < _utc_now_str()），换类型会牵动一大片比较
  语义。迁移期先保持形状，换类型作为后续独立改动。

lastrowid 在 PG 上没有对应物，用 insert_returning_id 显式取。
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger("ex-memory")

DIALECT_SQLITE = "sqlite"
DIALECT_POSTGRES = "postgres"

# datetime('now') 在 PG 上的等价物。两边都产出 'YYYY-MM-DD HH:MM:SS' 形式的
# UTC 文本，保证既有的字符串比较逻辑不变。
_PG_NOW = "to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')"


def integrity_errors() -> tuple:
    """两种驱动的唯一约束冲突异常。

    业务代码不该关心用的是哪个驱动，但 except 子句必须写具体类型
    （项目规范：不静默吞异常），所以在这里统一给出。
    """
    errors: list[type] = [sqlite3.IntegrityError]
    try:
        import psycopg

        errors.append(psycopg.errors.IntegrityError)
    except ImportError:
        pass
    return tuple(errors)


_pool: Any = None
_pool_lock = threading.Lock()


def dialect() -> str:
    import config

    return DIALECT_POSTGRES if config.DATABASE_URL else DIALECT_SQLITE


def _translate(sql: str) -> str:
    """把 SQLite 方言的 SQL 翻成当前方言。"""
    if dialect() == DIALECT_SQLITE:
        return sql
    upper = sql.upper()
    for unsupported in ("INSERT OR REPLACE", "INSERT OR IGNORE"):
        if unsupported in upper:
            # 不做静默翻译：ON CONFLICT 的语义要按表的约束来定，
            # 猜错会变成「悄悄没写进去」这类最难查的问题
            raise ValueError(
                f"{unsupported} 是 SQLite 专有语法，无法安全翻译到 Postgres。"
                "请改用两边都支持的写法，或按方言分支处理。"
            )
    sql = sql.replace("datetime('now')", _PG_NOW)
    # 只翻占位符，不动字符串字面量里的问号
    out, in_str, quote = [], False, ""
    for ch in sql:
        if in_str:
            out.append(ch)
            if ch == quote:
                in_str = False
            continue
        if ch in ("'", '"'):
            in_str, quote = True, ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


class _Cursor:
    """统一游标。屏蔽两种驱动在 rowcount / 行访问上的差异。"""

    def __init__(self, raw: Any):
        self._raw = raw

    def fetchone(self) -> Any:
        return self._raw.fetchone()

    def fetchall(self) -> list:
        return self._raw.fetchall()

    def __iter__(self):
        return iter(self._raw)

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount

    @property
    def lastrowid(self) -> Any:
        # PG 没有 lastrowid，需要用 insert_returning_id
        return getattr(self._raw, "lastrowid", None)


class Connection:
    """统一连接。业务代码写 SQLite 方言，这里按需翻译。"""

    def __init__(self, raw: Any, dialect_name: str):
        self._raw = raw
        self.dialect = dialect_name

    def execute(self, sql: str, params: tuple = ()) -> _Cursor:
        translated = _translate(sql)
        if self.dialect == DIALECT_SQLITE:
            return _Cursor(self._raw.execute(translated, params))
        cur = self._raw.cursor()
        cur.execute(translated, params)
        return _Cursor(cur)

    def insert_returning_id(self, sql: str, params: tuple = ()) -> Any:
        """插入并取自增主键。lastrowid 在 PG 上不存在，必须 RETURNING。"""
        if self.dialect == DIALECT_SQLITE:
            return self.execute(sql, params).lastrowid
        cur = self._raw.cursor()
        cur.execute(_translate(sql) + " RETURNING id", params)
        row = cur.fetchone()
        return row["id"] if isinstance(row, dict) else row[0]

    def executescript(self, sql: str) -> None:
        if self.dialect == DIALECT_SQLITE:
            self._raw.executescript(sql)
            return
        cur = self._raw.cursor()
        cur.execute(sql)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


def _pg_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            import config
            from psycopg_pool import ConnectionPool
            from psycopg.rows import dict_row

            _pool = ConnectionPool(
                config.DATABASE_URL,
                min_size=1,
                max_size=config.DATABASE_POOL_SIZE,
                kwargs={"row_factory": dict_row},
                open=True,
            )
        return _pool


@contextmanager
def connect(sqlite_path: Optional[Path] = None) -> Iterator[Connection]:
    """取一个连接。Postgres 走连接池，SQLite 每次新建。"""
    if dialect() == DIALECT_POSTGRES:
        with _pg_pool().connection() as raw:
            yield Connection(raw, DIALECT_POSTGRES)
        return

    if sqlite_path is None:
        raise ValueError("SQLite 模式需要给出数据库路径")
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(sqlite_path))
    raw.row_factory = sqlite3.Row
    try:
        yield Connection(raw, DIALECT_SQLITE)
    finally:
        raw.close()


def reset_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception:  # noqa: BLE001
                pass
            _pool = None


def migration_dir() -> Path:
    """当前方言的迁移目录。

    两种方言各一套 SQL：建表语法差异太大（AUTOINCREMENT vs GENERATED、
    ALTER TABLE 能力不同），硬翻译比维护两份更容易出错。
    """
    root = Path(__file__).resolve().parent.parent / "migrations"
    specific = root / dialect()
    return specific if specific.exists() else root
