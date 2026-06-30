#!/usr/bin/env python3
"""Dam release collector.

Reads the list of dams from the database, queries each dam's release
schedule (a CWMS time series) via the cwms-python package, and inserts the
returned readings into the dam_readings table.

Logging is emitted as JSON: informational/warning records go to STDOUT and
error/critical records go to STDERR.
"""

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import cwms
import pandas as pd
import psycopg2
import structlog


def configure_logging() -> None:
    """Configure structlog to emit JSON, splitting INFO/WARNING to STDOUT and
    ERROR/CRITICAL to STDERR."""
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    # Only records below ERROR (DEBUG/INFO/WARNING) go to STDOUT.
    stdout_handler.addFilter(lambda record: record.levelno < logging.ERROR)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(logging.ERROR)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(stdout_handler)
    root_logger.addHandler(stderr_handler)
    root_logger.setLevel(logging.INFO)


log = structlog.get_logger("dam_release_collector")


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([dhm])\s*$", re.IGNORECASE)
_DURATION_UNITS = {"d": "days", "h": "hours", "m": "minutes"}


def parse_datetime(value: str) -> datetime:
    """Parse an ISO-8601 timestamp. Naive values are assumed to be UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_duration(value: str) -> timedelta:
    """Parse a duration like '1h', '30m', or '2d'."""
    match = _DURATION_RE.match(value)
    if not match:
        raise argparse.ArgumentTypeError(
            f"invalid duration {value!r}; use forms like '1h', '30m', '2d'"
        )
    amount, unit = int(match.group(1)), match.group(2).lower()
    return timedelta(**{_DURATION_UNITS[unit]: amount})


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect dam release readings into the database."
    )
    parser.add_argument(
        "--start",
        type=parse_datetime,
        metavar="ISO8601",
        help="Start of the time window (e.g. '2026-01-01' or "
        "'2026-01-01T12:00:00+00:00'). Naive times are treated as UTC.",
    )
    parser.add_argument(
        "--end",
        type=parse_datetime,
        metavar="ISO8601",
        help="End of the time window. Naive times are treated as UTC.",
    )
    parser.add_argument(
        "--lookback",
        type=parse_duration,
        metavar="DURATION",
        help="Pull only the most recent window, e.g. '1h'. Sets start to "
        "now minus DURATION and end to now. Cannot be combined with --start.",
    )
    args = parser.parse_args(argv)

    if args.lookback is not None:
        if args.start is not None:
            parser.error("--lookback cannot be combined with --start")
        now = datetime.now(timezone.utc)
        args.start = now - args.lookback
        if args.end is None:
            args.end = now

    if args.start is not None and args.end is not None and args.start > args.end:
        parser.error("--start must be before --end")

    return args


def connect():
    """Open a database connection using the DB_* environment variables."""
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def get_dams(conn):
    """Return the list of dams as a list of dicts."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM dams;")
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def reading_exists(cur, dam_id, timestamp) -> bool:
    """Return True if a reading already exists for this dam and timestamp."""
    cur.execute(
        "SELECT 1 FROM dam_readings WHERE dam_id = %s AND timestamp = %s LIMIT 1;",
        (dam_id, timestamp),
    )
    return cur.fetchone() is not None


def insert_reading(cur, dam_id, timestamp, reading, quality) -> None:
    """Insert a single reading; collection_timestamp is set to NOW()."""
    cur.execute(
        """
        INSERT INTO dam_readings (dam_id, timestamp, collection_timestamp, reading, quality)
        VALUES (%s, %s, NOW(), %s, %s);
        """,
        (dam_id, timestamp, reading, quality),
    )


def collect_dam(conn, dam, begin=None, end=None) -> None:
    """Fetch the release schedule for a single dam and store its readings."""
    dam_id = dam["id"]
    ts_id = dam["metric_id"]
    office_id = dam["office"]

    dam_log = log.bind(
        dam_id=dam_id, dam_name=dam.get("name"), ts_id=ts_id, office_id=office_id
    )
    dam_log.info(
        "querying dam release schedule",
        begin=begin.isoformat() if begin else None,
        end=end.isoformat() if end else None,
    )

    data = cwms.get_timeseries(ts_id=ts_id, office_id=office_id, begin=begin, end=end)
    df = data.df

    inserted = 0
    skipped = 0
    with conn.cursor() as cur:
        for row in df.itertuples(index=False):
            timestamp = getattr(row, "_0")  # 'date-time' is not a valid identifier
            value = getattr(row, "value")
            quality_code = getattr(row, "_2")  # 'quality-code'

            # Normalize pandas types for psycopg2.
            timestamp = timestamp.to_pydatetime()
            reading = None if pd.isna(value) else float(value)
            quality = None if pd.isna(quality_code) else int(quality_code)

            if reading_exists(cur, dam_id, timestamp):
                dam_log.warning(
                    "reading already exists, skipping",
                    timestamp=timestamp.isoformat(),
                )
                skipped += 1
                continue

            insert_reading(cur, dam_id, timestamp, reading, quality)
            inserted += 1

    conn.commit()
    dam_log.info("finished dam", readings=len(df), inserted=inserted, skipped=skipped)


def main(argv=None) -> int:
    args = parse_args(argv)
    configure_logging()
    log.info(
        "starting dam release collector",
        start=args.start.isoformat() if args.start else None,
        end=args.end.isoformat() if args.end else None,
    )

    try:
        conn = connect()
    except Exception:
        log.error("failed to connect to database", exc_info=True)
        return 1

    failures = 0
    try:
        dams = get_dams(conn)
        log.info("retrieved dams", count=len(dams))

        for dam in dams:
            try:
                collect_dam(conn, dam, begin=args.start, end=args.end)
            except Exception:
                conn.rollback()
                failures += 1
                log.error(
                    "failed to collect dam",
                    dam_id=dam.get("id"),
                    dam_name=dam.get("name"),
                    exc_info=True,
                )
    finally:
        conn.close()

    log.info("collector finished", failures=failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
