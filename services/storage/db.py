"""
services/storage/db.py
======================
Engine and session management. The only module that knows how to reach the
database.

Postgres only. SQLite was dropped deliberately: pgvector exists only on
Postgres, so supporting both would mean two topic-deduplication code paths —
an indexed similarity query and a brute-force numpy scan — each needing its own
tests. And a container filesystem is ephemeral, so a SQLite file is destroyed on
every deploy.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger(__name__)

_engine: Engine | None = None
_Session: sessionmaker | None = None
_lock = threading.Lock()


class DatabaseNotConfigured(RuntimeError):
    """DATABASE_URL is missing or unusable."""


def normalise_url(raw: str) -> str:
    """Turn a pasted connection string into one SQLAlchemy + psycopg can use.

    Two adjustments, both worth knowing about:

    * `postgresql://` -> `postgresql+psycopg://` so SQLAlchemy picks psycopg 3
      rather than looking for psycopg2.
    * Neon's `-pooler` endpoint is dropped. The pooler runs PgBouncer in
      transaction mode, where a session-level `pg_try_advisory_lock` is not
      held across statements — and that lock is what stops three Gunicorn
      workers each running the publish reconciler and sending every post three
      times. A long-lived Flask process with SQLAlchemy's own pool does not
      need PgBouncer anyway.
    """
    url = raw.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]

    if "-pooler." in url:
        url = url.replace("-pooler.", ".")
        log.info(
            "Using the direct Postgres endpoint instead of the pooled one: the "
            "publish reconciler needs session-level advisory locks, which "
            "PgBouncer's transaction pooling does not hold."
        )
    return url


def database_url() -> str:
    # engine.config is the only module that reads .env, so go through it —
    # importing it here is also what guarantees .env has been loaded.
    from engine.config import config

    raw = (config.DATABASE_URL or os.getenv("DATABASE_URL", "")).strip()
    if not raw:
        raise DatabaseNotConfigured(
            "DATABASE_URL is not set. Add your Postgres connection string to .env:\n"
            "  DATABASE_URL=postgresql://user:password@host/dbname?sslmode=require"
        )
    return normalise_url(raw)


def get_engine() -> Engine:
    """The process-wide engine. Created lazily so imports stay cheap."""
    global _engine, _Session
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        _engine = create_engine(
            database_url(),
            pool_pre_ping=True,      # Neon idles connections out; re-check before use
            pool_size=5,
            max_overflow=5,
            pool_recycle=280,        # under Neon's ~5 min idle timeout
            future=True,
            connect_args={"connect_timeout": 15},
        )
        _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        return _engine


#: Transient connect-time failures worth retrying. Neon suspends an idle
#: endpoint and DNS for a waking one can fail outright for a second or two —
#: pool_pre_ping only revalidates a connection that already exists, so it does
#: not help here.
_TRANSIENT = (
    "getaddrinfo failed",
    "failed to resolve host",
    "temporary failure in name resolution",
    "connection timed out",
    "server closed the connection unexpectedly",
    "could not connect to server",
)

#: Four attempts with a growing gap gives about nine seconds before giving up.
#: Three gave four and a half, which measurably was not enough: a Neon endpoint
#: resuming from suspend regularly failed all three and surfaced as a hard
#: "database unreachable" that was gone on the next request.
CONNECT_ATTEMPTS = 4
CONNECT_BACKOFF = 1.5


def _is_transient(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(signal in message for signal in _TRANSIENT)


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transactional session. Commits on success, rolls back on any exception.

    Callers must not swallow the exception: a write that fails has to surface,
    not disappear into a False return that nobody checks. A transient failure
    to reach the database is retried first, so a suspended endpoint waking up
    does not look like a real error.
    """
    get_engine()
    assert _Session is not None

    session = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            session = _Session()
            session.connection()      # force the connect here, not mid-transaction
            break
        except Exception as exc:
            if session is not None:
                session.close()
            if attempt == CONNECT_ATTEMPTS or not _is_transient(exc):
                raise
            delay = CONNECT_BACKOFF * attempt
            log.warning("Database unreachable (%s); retrying in %.1fs [%d/%d]",
                        str(exc)[:90], delay, attempt, CONNECT_ATTEMPTS)
            time.sleep(delay)

    assert session is not None
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_schema() -> None:
    """Create the pgvector extension and every table that does not exist yet.

    Alembic owns migrations for an existing database; this is for a fresh one.
    """
    from services.storage.models import Base

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)


@contextmanager
def advisory_lock(key: int) -> Iterator[bool]:
    """Try to take a Postgres session-level advisory lock.

    Yields True if this process now holds it, False if another already does.
    Used so exactly one worker runs the publish reconciler, however many
    Gunicorn workers are serving requests.
    """
    engine = get_engine()
    conn = engine.connect()
    try:
        acquired = bool(
            conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        )
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
    finally:
        conn.close()


def healthcheck() -> tuple[bool, str]:
    """(ok, detail) — used by engine.health at startup.

    Retries transient failures on the same schedule as `session_scope`. A
    serverless Postgres wakes from idle in a second or two, and the first
    connection after that idle period often fails DNS or times out. Reporting
    that as "database not reachable" blocked startup entirely — the app
    refused to boot over a blip that was gone by the time anyone read the
    error. A configuration problem still fails immediately, because retrying
    a wrong connection string just makes the operator wait longer for the same
    answer.
    """
    last = "the database did not respond"
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            with get_engine().connect() as conn:
                version = conn.execute(text("SELECT version()")).scalar() or ""
                has_vector = conn.execute(
                    text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                ).scalar()
            detail = version.split(" on ")[0]
            if not has_vector:
                return False, f"{detail} — pgvector extension is not installed"
            return True, detail
        except DatabaseNotConfigured as exc:
            return False, str(exc).splitlines()[0]
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc)[:160]}"
            if attempt == CONNECT_ATTEMPTS or not _is_transient(exc):
                break
            delay = CONNECT_BACKOFF * attempt
            log.info("Database not up yet (%s); retrying in %.1fs (%d/%d).",
                     str(exc)[:90], delay, attempt, CONNECT_ATTEMPTS)
            time.sleep(delay)
    return False, last


def dispose() -> None:
    """Close pooled connections. For tests and clean shutdown."""
    global _engine, _Session
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _Session = None
