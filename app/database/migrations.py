"""Additive schema reconciliation for a database that already exists.

Why this module exists
----------------------
The application creates its schema with ``Base.metadata.create_all``. That call is
idempotent about *tables* and completely blind to *columns*: it creates a table that is
missing and leaves a table that exists exactly as it found it. So the moment a model
grows a column - ``EvidenceFile.provenance``, say - two populations diverge. Someone who
clones the repository fresh gets the new column, because the table is created from the
current model. Someone who pulls the same commit onto the ``data/audit.db`` they have
been using gets the *old* table, and the first query that names the new column fails
with ``OperationalError: no such column``. The application still starts; it breaks on
use, which is the worst place to break.

:func:`reconcile_schema` closes that gap. For every mapped table that already exists it
compares the model's columns against the live columns and issues one
``ALTER TABLE ... ADD COLUMN`` per missing column, then reports what it did. Running it
on a fresh database, or twice in a row, changes nothing.

This is a deliberate lightweight substitute for Alembic, not a replacement for it
--------------------------------------------------------------------------------
A single-user research prototype that ships as a repository someone clones does not
need a migration *history*: there is no fleet of databases at different revisions, no
rollback requirement, and no team coordinating concurrent migrations. What it needs is
for an existing SQLite file to survive ``git pull``. Reconciling the live schema against
the models covers that case in one function with no revision files to keep in step with
the code, and it can never be out of date with the models, because the models *are* the
specification.

The price is that it handles exactly one kind of change. It does **not** handle:

* **Column renames** - a rename looks like "one column missing, one column unknown", and
  this module only ever sees the first half. It will add the new name and leave the old
  column in place holding the data. Renames need Alembic.
* **Type changes** - an existing column is never inspected for type, never altered and
  never rebuilt. A column whose model type changed keeps its original storage type.
* **Data migrations** - nothing is backfilled. A column added here is NULL for every
  pre-existing row, and any code that reads it must tolerate NULL (see the nullability
  rule below).
* **Constraint changes** - no constraint is added, dropped or modified. A new column that
  carries a ``ForeignKey``, ``UniqueConstraint`` or ``CHECK`` in the model is added as a
  plain column without it, because adding a constraint to a populated table can fail on
  the existing rows and is not an additive operation.
* **Dropping anything** - a column that exists in the database but not in the models is
  left untouched. Extra columns are harmless; a wrong ``DROP`` is not recoverable.
* **Indexes** - a column declared ``index=True`` is added without its index. That costs a
  scan where an index would have been used; it never costs correctness, and an existing
  database large enough for the difference to matter is past the point where this module
  should still be doing the work.

**Alembic is the upgrade path.** ``app/database/base.py`` already declares a
``NAMING_CONVENTION`` on the metadata precisely so that Alembic autogeneration produces
reversible migrations when this prototype needs any of the five capabilities above. At
that point this module should be deleted rather than extended: two mechanisms writing
DDL to the same database is worse than either one alone.

Failure policy
--------------
Every statement is attempted independently and no failure propagates. A schema
reconciliation that cannot add a column must not prevent the application from starting,
because a running application with one missing column is diagnosable and a process that
refuses to boot is not. Failures are logged at WARNING and reported in the return value.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from sqlalchemy import Column, Engine, MetaData, Table, inspect
from sqlalchemy.exc import SQLAlchemyError

from app.database.base import Base, engine as default_engine

logger = logging.getLogger(__name__)

__all__ = ["reconcile_schema"]


def reconcile_schema(
    engine: Optional[Engine] = None, metadata: Optional[MetaData] = None
) -> List[str]:
    """Add every model column missing from an existing table. Additive only.

    Tables that do not exist yet are skipped: ``create_all`` builds those whole, so
    there is nothing to reconcile and issuing DDL against them would be a race with it.
    Call this *after* ``create_all`` (:func:`app.database.base.init_db` does).

    :param engine: database to reconcile; defaults to the application engine.
    :param metadata: the schema to reconcile *towards*; defaults to ``Base.metadata``,
        which is the only value the application ever passes. It is a parameter so that
        the desired schema is an explicit input rather than a global read halfway down
        this function, which is what lets ``tests/test_migrations.py`` stand up a
        database that is one column behind a schema without mutating the real models.
    :returns: one human-readable line per change actually applied, plus one per column
        that could not be added. An empty list means the live schema already matched the
        models - the normal result on a fresh clone and on the second run.
    """
    target = engine if engine is not None else default_engine

    # Importing the models is what populates ``Base.metadata``; without it this function
    # would cheerfully report that a database with no registered mappers is up to date.
    from app.database import models  # noqa: F401

    schema = metadata if metadata is not None else Base.metadata

    changes: List[str] = []
    try:
        inspector = inspect(target)
        existing_tables = set(inspector.get_table_names())
    except SQLAlchemyError as exc:
        logger.warning("Schema reconciliation skipped - could not inspect the database: %s", exc)
        return changes

    with target.connect() as connection:
        for table in schema.sorted_tables:
            if table.name not in existing_tables:
                continue
            try:
                live_columns = {column["name"] for column in inspector.get_columns(table.name)}
            except SQLAlchemyError as exc:
                logger.warning("Could not read columns of %s: %s", table.name, exc)
                continue

            for column in table.columns:
                if column.name in live_columns:
                    continue
                changes.extend(_add_column(connection, target.dialect, table, column))

    if changes:
        logger.info("Schema reconciliation applied %d change(s).", len(changes))
    return changes


# ---- one column
def _add_column(connection: Any, dialect: Any, table: Table, column: Column) -> List[str]:
    """Issue a single ``ADD COLUMN`` and describe the outcome. Never raises."""
    label = "{0}.{1}".format(table.name, column.name)

    if column.primary_key:
        # A primary key cannot be introduced by ALTER TABLE on either backend, and a
        # table missing its own primary key is a corruption this module cannot repair.
        message = "{0}: SKIPPED - a primary key column cannot be added to an existing table".format(label)
        logger.warning(message)
        return [message]

    try:
        ddl, note = _add_column_ddl(dialect, table, column)
    except Exception as exc:  # pragma: no cover - a type with no DDL rendering
        message = "{0}: SKIPPED - could not render DDL for type {1!r}: {2}".format(
            label, type(column.type).__name__, exc
        )
        logger.warning(message)
        return [message]

    try:
        # ``exec_driver_sql`` rather than ``text()``: DDL is a literal string built
        # here from model metadata, and ``text()`` would try to read a ``:token`` in a
        # rendered server default as a bind parameter.
        connection.exec_driver_sql(ddl)
        # Committed per column rather than per run so that a later failure cannot roll
        # back columns that were added successfully. Partial progress is the desired
        # outcome here: the next start reconciles whatever remains.
        connection.commit()
    except SQLAlchemyError as exc:
        connection.rollback()
        message = "{0}: FAILED - {1}".format(label, exc.__class__.__name__)
        logger.warning("Could not add column %s: %s", label, exc)
        return [message]

    rendered_type = column.type.compile(dialect=dialect)
    message = "{0}: added column {1}".format(label, rendered_type)
    if note:
        message = "{0} ({1})".format(message, note)
    logger.info("Schema reconciliation: %s", message)
    return [message]


def _add_column_ddl(dialect: Any, table: Table, column: Column) -> Tuple[str, Optional[str]]:
    """Render ``ALTER TABLE ... ADD COLUMN ...`` for one column, plus any caveat.

    The column type is rendered by the dialect's own type compiler rather than from a
    lookup table in this file, which is what makes the same code correct on SQLite
    (``VARCHAR(64)``, ``BLOB``) and on PostgreSQL (``VARCHAR(64)``, ``BYTEA``).
    """
    preparer = dialect.identifier_preparer
    parts: List[str] = [preparer.format_column(column), column.type.compile(dialect=dialect)]
    note: Optional[str] = None

    default_sql = _server_default_sql(dialect, column)
    if column.nullable:
        if default_sql is not None:
            parts.append("DEFAULT {0}".format(default_sql))
    elif default_sql is not None:
        parts.append("NOT NULL DEFAULT {0}".format(default_sql))
    else:
        # A NOT NULL column with no server-side default cannot be added to a table that
        # already has rows: there is no value to put in them. Adding it nullable keeps
        # the data and lets the application start; new rows still get the model's
        # Python-side default, so only the pre-existing rows hold NULL. The alternative
        # - inventing a backfill value - would silently fabricate audit data, which is
        # exactly what this system must never do.
        note = (
            "relaxed to NULLABLE: the model declares NOT NULL with no server default, so "
            "existing rows have no value to backfill - rows created from now on get the "
            "model default"
        )

    if column.foreign_keys:
        # Rendered without the REFERENCES clause; see the module docstring on constraints.
        note = "; ".join(
            filter(None, [note, "foreign key not applied - constraint changes are out of scope"])
        )

    ddl = "ALTER TABLE {0} ADD COLUMN {1}".format(preparer.format_table(table), " ".join(parts))
    return ddl, note


def _server_default_sql(dialect: Any, column: Column) -> Optional[str]:
    """The column's server-side default as SQL text, or ``None`` if it has none.

    Only a *server* default counts. A ``default=`` on the model is evaluated in Python at
    insert time and is invisible to ``ALTER TABLE``, which is precisely why a NOT NULL
    column carrying only a Python default ends up relaxed to nullable above.

    The rendering is delegated to the dialect's own DDL compiler rather than written out
    here, because the quoting is not obvious: ``server_default="SYNTHETIC"`` is a *string
    literal* and has to reach the database as ``DEFAULT 'SYNTHETIC'``, while
    ``server_default=text("now()")`` is an *expression* and must not be quoted. The
    compiler already makes that distinction correctly for every backend.
    """
    if column.server_default is None:
        return None
    try:
        return dialect.ddl_compiler(dialect, None).get_column_default_string(column)
    except Exception:  # pragma: no cover - exotic default constructs
        logger.warning(
            "Could not render the server default for %s; adding the column without it.",
            column.name,
        )
        return None
