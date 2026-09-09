"""Backups and ledger verification — the two jobs a four-year obligation actually needs.

    python scripts/retention.py backup            # one dump, uploaded under object lock
    python scripts/retention.py verify            # recompute every tenant's hash chain
    python scripts/retention.py catalogue         # every backup version, incl. masked ones
    python scripts/retention.py restore --drill   # newest backup into a scratch database
    python scripts/retention.py schedule          # both, on their own intervals, forever

Every week since 10 has ended with the same line on the roadmap: `postgres-data` is a
volume on one host, the retention obligation runs for years, and a volume is not a backup.
This is that job.

---------------------------------------------------------------------------------------
**Why object lock and not a lifecycle rule.**

The threat a customs record retention obligation is actually against is not disk failure.
It is a dispute in year four in which the party holding the records also has the ability
to change them. Versioning alone does not answer that: a credential that can write to the
bucket can delete every version, and the same credential runs the process that produced
the data. S3 object lock in COMPLIANCE mode is the only setting where the answer to "could
this have been altered" is no rather than "no, according to the party who could have".

GOVERNANCE mode is the obvious-looking alternative and it is the wrong one: it can be
bypassed by anyone holding `s3:BypassGovernanceRetention`, which in a single-tenant
on-prem MinIO means the operator. A retention control that the operator can lift is a
retention policy, not a retention control.

The consequence is real and is the point: **nothing here prunes.** An object written today
cannot be deleted by this script, by the operator, or by MinIO's own lifecycle engine
until its retain-until date passes. Storage grows monotonically for the length of the
obligation. That is the cost of the guarantee, and a `--retention-days` short enough to
avoid it would be a deployment that has bought nothing.

---------------------------------------------------------------------------------------
**Why there is a restore verb, and why it is a drill rather than a recovery tool.**

Week 15 took backups, proved they were immutable against a live MinIO, and shipped. The
week 16 checklist opened with the reason that was not enough: *a backup nobody has restored
is a file.* Everything the backup path asserts — that the dump is complete, that custom
format round-trips, that `--no-owner` really does free it from the roles it was taken
under, that the ledger survives the trip — is an assertion about a restore that had never
happened.

So `restore` exists to *fail*, in a scratch database, on a schedule, while there is nobody
waiting. It downloads a real object, checks the bytes against the digest recorded when they
were written, restores into a database that is created for the purpose, re-verifies every
tenant's hash chain **inside the restored copy**, and reports how long each stage took —
because "we can restore" and "we can restore before the hearing" are different claims and
only one of them is answered by a working command.

It refuses to restore over the database it dumped from. That guard is not paranoia about a
typo: the natural way to test a restore is to point it at the database you already have,
and doing so once would overwrite production with a copy of itself from last night.

---------------------------------------------------------------------------------------
**Why verification does not write to the ledger.**

The obvious design records each verification as an `audit_ledger` event. It is a trap: the
ledger is a hash chain, so writing the result of verifying the chain extends the chain, and
the next verification covers rows that exist because of the previous one. Worse, the
attestation would be inside the structure it attests to — an altered ledger would carry an
altered record of having been checked. The result goes to stdout as JSON for the log
pipeline, and the exit code carries it to whatever runs this. Both live outside the
database.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from services.api.src.config import get_settings
from services.api.src.ledger import verify_chain

if TYPE_CHECKING:
    from uuid import UUID

DEFAULT_DSN = "postgresql+psycopg://drawbridge:drawbridge@localhost:5432/drawbridge"

#: 19 CFR 163.4 requires records be kept five years from the date of entry; GCC Common
#: Customs Law Art. 175 runs to five years as well. A backup taken today may contain a
#: claim entered today, so the clock on the *backup* starts now and the obligation on the
#: newest row inside it is what sets the length. 1830 days is five years with the leap.
#:
#: Not a hard-coded constant, because a deployment under a longer statutory obligation must
#: be able to raise it — and because a number this consequential should be visible in the
#: compose file rather than only in this docstring.
DEFAULT_RETENTION_DAYS = 1830

DEFAULT_BACKUP_INTERVAL = 24 * 60 * 60.0
DEFAULT_VERIFY_INTERVAL = 60 * 60.0


class RetentionError(RuntimeError):
    """A job could not complete. Names the step, never a credential."""


#: Tables a restore has to bring back non-empty before it counts as one.
#:
#: Chosen as the four the obligation is actually about rather than the four with the most
#: rows: the ledger is the audit record, `claims` and `entry_lines` are what a refund was
#: computed from, and `tariff_lines` is the schedule those figures were justified against.
#: A dump that restored everything except one of these would satisfy a row count over the
#: whole database and satisfy nothing an auditor asks for.
RESTORE_CHECK_TABLES = ("audit_ledger", "claims", "entry_lines", "tariff_lines")


def _dsn() -> str:
    return os.environ.get("DRAWBRIDGE_OWNER_DATABASE_URL") or os.environ.get(
        "DRAWBRIDGE_DATABASE_URL", DEFAULT_DSN
    )


def _sync_dsn(dsn: str) -> str:
    """The synchronous driver. Alembic and the API use asyncpg; this does not."""
    return dsn.replace("+asyncpg", "+psycopg")


def _libpq_url(dsn: str) -> str:
    """A SQLAlchemy URL as `pg_dump` wants it.

    `pg_dump` speaks libpq and has never heard of `postgresql+psycopg://`.
    """
    return dsn.replace("+asyncpg", "").replace("+psycopg", "")


def _s3_client() -> Any:
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=os.environ.get("DRAWBRIDGE_AWS_REGION", "us-east-1"),
        # MinIO wants path style; so does anything self-hosted behind one hostname.
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
    )


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def dump(dsn: str, destination: Path) -> tuple[int, str]:
    """`pg_dump` the whole database. Returns (bytes, sha256).

    Custom format, compressed, `--no-owner --no-privileges`: a restore into a fresh
    cluster should not depend on `drawbridge_app` and `drawbridge` already existing with
    the same oids, because in the restore that matters they will not.

    The password never appears in an argument. `pg_dump` reads `PGPASSWORD` from the
    environment of the child, which keeps it out of the host's process list — where every
    other user on the machine can read it, and where a backup job is exactly the sort of
    long-running process someone happens to look at.
    """
    url = _libpq_url(dsn)
    if shutil.which("pg_dump") is None:
        msg = "pg_dump is not on PATH; this job needs the postgresql-client package"
        raise RetentionError(msg)

    completed = subprocess.run(
        [
            "pg_dump",
            "--dbname",
            url,
            "--format=custom",
            "--compress=9",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        # stderr can carry the host and database name, which are not credentials, but not
        # the password — libpq never echoes it.
        tail = completed.stderr.strip().splitlines()[-1:] or ["no output"]
        msg = f"pg_dump exited {completed.returncode}: {tail[0]}"
        raise RetentionError(msg)

    digest = hashlib.sha256()
    with destination.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return destination.stat().st_size, digest.hexdigest()


def backup(*, bucket: str, retention_days: int, dsn: str | None = None) -> dict[str, Any]:
    """Dump, upload under a retain-until date, and report what was written.

    The dump goes to a temporary file rather than straight to a multipart upload. A
    streamed dump cannot be hashed before it is stored, so the object's own checksum would
    be the only description of it — and "the bytes we uploaded hash to the hash we computed
    while uploading" is not a check of anything. Written first, hashed, then uploaded, so
    the digest is a property of a file that exists.
    """
    resolved = _sync_dsn(dsn or _dsn())
    stamp = _now()
    key = f"postgres/{stamp:%Y/%m/%d}/drawbridge-{stamp:%Y%m%dT%H%M%SZ}.dump"
    retain_until = stamp + dt.timedelta(days=retention_days)

    with tempfile.TemporaryDirectory(prefix="drawbridge-backup-") as workdir:
        path = Path(workdir) / "drawbridge.dump"
        size, sha256 = dump(resolved, path)

        client = _s3_client()
        try:
            with path.open("rb") as handle:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=handle,
                    ObjectLockMode="COMPLIANCE",
                    ObjectLockRetainUntilDate=retain_until,
                    # Belt and braces with the lock: the checksum is verified by the
                    # server on write, so a truncated upload fails here rather than
                    # becoming an object that cannot be deleted and cannot be restored.
                    ChecksumAlgorithm="SHA256",
                    Metadata={
                        "sha256": sha256,
                        "pg-dump-format": "custom",
                        "taken-at": stamp.isoformat(),
                    },
                )
        except (BotoCoreError, ClientError) as exc:
            msg = (
                f"could not write {key} to {bucket}: {type(exc).__name__}. If this is "
                "'InvalidRequest', the bucket was created without object lock — a lock "
                "cannot be enabled on an existing bucket, so it has to be recreated."
            )
            raise RetentionError(msg) from exc

    return {
        "job": "backup",
        "bucket": bucket,
        "key": key,
        "bytes": size,
        "sha256": sha256,
        "object_lock": "COMPLIANCE",
        "retain_until": retain_until.isoformat(),
        "taken_at": stamp.isoformat(),
    }


def _tenants(session: Session) -> list[UUID]:
    """Every tenant with at least one ledger entry.

    From the ledger rather than from `tenants`, because the job is verifying chains and a
    tenant with no chain has nothing to verify. It also means an offboarded tenant whose
    rows are retained is still checked, which is the case where the guarantee matters
    most: nobody is looking at that data day to day.
    """
    rows = session.execute(text("SELECT DISTINCT tenant_id FROM audit_ledger")).scalars().all()
    return list(rows)


def catalogue(*, bucket: str) -> dict[str, Any]:
    """Every backup **version**, and whether a delete marker is hiding the newest one.

    Not `list_objects`. Object lock protects the bytes and says nothing about visibility:
    on a versioned bucket `DeleteObject` succeeds against a locked object by writing a
    delete marker, and the object then disappears from an ordinary listing while the
    protected version sits underneath, undeletable. Measured against MinIO: `mc rm` on a
    COMPLIANCE-locked dump returns success and creates a marker; `mc rb --force` then fails
    on the same object with "is WORM protected and cannot be overwritten".

    So the failure this guards against is not losing a backup. It is looking for backups
    during an incident, seeing an empty bucket, and concluding there are none — when
    every one of them is present and one API call away. A restore procedure has to
    enumerate versions, which is what this does.
    """
    client = _s3_client()
    try:
        pages = client.get_paginator("list_object_versions").paginate(
            Bucket=bucket, Prefix="postgres/"
        )
        versions: list[dict[str, Any]] = []
        markers: list[dict[str, Any]] = []
        for page in pages:
            for item in page.get("Versions", []):
                versions.append(
                    {
                        "key": item["Key"],
                        "version_id": item["VersionId"],
                        "bytes": item["Size"],
                        "last_modified": item["LastModified"],
                        "is_latest": item["IsLatest"],
                    }
                )
            for item in page.get("DeleteMarkers", []):
                markers.append({"key": item["Key"], "version_id": item["VersionId"]})
    except (BotoCoreError, ClientError) as exc:
        msg = f"could not list versions in {bucket}: {type(exc).__name__}"
        raise RetentionError(msg) from exc

    masked = sorted({m["key"] for m in markers} & {v["key"] for v in versions})
    versions.sort(key=lambda v: str(v["last_modified"]), reverse=True)
    return {
        "job": "catalogue",
        "bucket": bucket,
        "versions": len(versions),
        "newest": versions[0] if versions else None,
        # A key here has a recoverable backup that an ordinary listing does not show.
        # Non-empty is not corruption; it is a restore that needs a version id.
        "masked_by_delete_marker": masked,
        "ok": bool(versions),
    }


def verify(*, dsn: str | None = None) -> dict[str, Any]:
    """Recompute every tenant's hash chain. Returns a report; never raises on a break.

    A broken chain is a finding, not an exception — the same posture `verify_chain` takes
    for one tenant, for the same reason: the job must go on to check the others. A break
    in tenant A is not a reason to leave tenant B unchecked, and it is precisely when
    something is wrong that the full picture matters.
    """
    engine = create_engine(_sync_dsn(dsn or _dsn()))
    findings: list[dict[str, Any]] = []
    with Session(engine) as session:
        # The ledger is append-only and readable by the owner role; RLS is bypassed here
        # deliberately and this job is the reason `scripts/` connects as the owner. A
        # per-tenant verification that could only see one tenant could not answer "is the
        # ledger intact", which is the question.
        for tenant_id in _tenants(session):
            result = verify_chain(session, tenant_id)
            findings.append({"tenant_id": str(tenant_id), **result})

    broken = [f for f in findings if not f["ok"]]
    return {
        "job": "verify",
        "checked_at": _now().isoformat(),
        "tenants": len(findings),
        "entries": sum(int(f.get("entries", 0)) for f in findings),
        "ok": not broken,
        "broken": broken,
    }


def newest_backup(*, bucket: str) -> dict[str, Any]:
    """The most recent backup version, by the time MinIO recorded it.

    From `catalogue` rather than `list_objects`, so a key hidden behind a delete marker is
    still a restore candidate. During an incident that is the difference between "there are
    no backups" and "there is a backup and it needs a version id".
    """
    listing = catalogue(bucket=bucket)
    newest = listing["newest"]
    if newest is None:
        msg = f"no backup versions under postgres/ in {bucket}"
        raise RetentionError(msg)
    return dict(newest)


def _fetch(client: Any, *, bucket: str, key: str, version_id: str | None, path: Path) -> str:
    """Download one object and check it against the digest taken before it was uploaded.

    The stored `sha256` metadata is the load-bearing part. S3's own ETag is not a checksum
    of the content for a multipart upload, and even where it is, it is computed by the same
    party that stored the object — comparing an object to its own ETag proves the transfer
    worked, not that the bytes are the ones `pg_dump` produced. The metadata digest was
    computed from a file on disk before any of this, which is what makes it evidence.
    """
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    try:
        response = client.get_object(**kwargs)
        digest = hashlib.sha256()
        with path.open("wb") as handle:
            for chunk in response["Body"].iter_chunks(1024 * 1024):
                handle.write(chunk)
                digest.update(chunk)
    except (BotoCoreError, ClientError) as exc:
        msg = f"could not read {key} from {bucket}: {type(exc).__name__}"
        raise RetentionError(msg) from exc

    recorded = (response.get("Metadata") or {}).get("sha256")
    actual = digest.hexdigest()
    if recorded and recorded != actual:
        msg = (
            f"{key} does not match the digest recorded when it was written "
            f"(recorded {recorded[:16]}..., got {actual[:16]}...). Do not restore this "
            "object; catalogue the bucket and pick another version."
        )
        raise RetentionError(msg)
    return actual


def _psql_admin(url: str, statement: str) -> None:
    """Run one statement against `postgres`, for the things that cannot run inside a
    transaction or inside the database they act on.

    CREATE DATABASE and DROP DATABASE are both in that set, which is why this exists rather
    than a SQLAlchemy call — SQLAlchemy opens a transaction, and Postgres refuses either
    statement inside one.
    """
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text(statement))
    finally:
        engine.dispose()


#: How often the scheduled loop restores a backup into a scratch database.
#:
#: Weekly, which is a deliberate compromise rather than an obvious number. A drill costs a
#: full restore of the database and the disk to hold it, so nightly would double the
#: storage the deployment needs to survive a routine day. But the failure it guards against
#: — a dump that has been silently unrestorable for months — is only bounded by how long it
#: can go unnoticed, and "we last proved this in the spring" is not an answer to a
#: production question.
DEFAULT_DRILL_INTERVAL = 7 * 24 * 60 * 60.0


def restore(
    *,
    bucket: str,
    dsn: str | None = None,
    key: str | None = None,
    version_id: str | None = None,
    scratch: str = "drawbridge_restore_drill",
    keep: bool = False,
) -> dict[str, Any]:
    """Restore a backup into a scratch database and check what came back.

    Four stages, each timed, because the number a recovery plan needs is not "does it work"
    but "how long". Download, restore, count, verify.

    The verification is the point and it is deliberately not a row count alone. Row counts
    prove `pg_restore` moved data; recomputing every tenant's ledger hash chain **in the
    restored database** proves the data that came back is the data that went in. A backup
    that restores a corrupted ledger restores a record nobody can rely on, and it would
    pass every check short of this one.
    """
    source = _sync_dsn(dsn or _dsn())
    source_url = make_url(source)
    if scratch == source_url.database:
        msg = (
            f"refusing to restore into {scratch!r}, which is the database this backup was "
            "taken from. A restore drill that overwrites production is not a drill."
        )
        raise RetentionError(msg)
    for tool in ("pg_restore", "psql"):
        if shutil.which(tool) is None:
            msg = f"{tool} is not on PATH; this job needs the postgresql-client package"
            raise RetentionError(msg)

    client = _s3_client()
    if key is None:
        newest = newest_backup(bucket=bucket)
        key, version_id = newest["key"], newest["version_id"]

    # `render_as_string(hide_password=False)`, not `str()`. SQLAlchemy's `__str__` renders
    # the password as `***`, which is the right default everywhere except here — the first
    # run of this drill failed with "password authentication failed", from a URL that reads
    # correctly in the traceback and cannot connect.
    admin_url = source_url.set(database="postgres").render_as_string(hide_password=False)
    scratch_url = source_url.set(database=scratch).render_as_string(hide_password=False)
    timings: dict[str, float] = {}
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="drawbridge-restore-") as workdir:
        path = Path(workdir) / "restore.dump"
        sha256 = _fetch(client, bucket=bucket, key=key, version_id=version_id, path=path)
        timings["download_seconds"] = round(time.monotonic() - started, 2)

        # Dropped first, so a drill that died halfway through the last run does not leave a
        # half-restored database that the next one reports as a success.
        _psql_admin(admin_url, f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        _psql_admin(admin_url, f'CREATE DATABASE "{scratch}"')

        mark = time.monotonic()
        completed = subprocess.run(
            [
                "pg_restore",
                "--dbname",
                _libpq_url(scratch_url),
                "--no-owner",
                "--no-privileges",
                # Extensions and roles the scratch database does not have produce errors
                # that are noise, not failure. The real check is the ledger, below, so a
                # non-zero exit is reported rather than raised.
                "--no-comments",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        timings["restore_seconds"] = round(time.monotonic() - mark, 2)

    warnings = [
        line for line in completed.stderr.splitlines() if line.strip().startswith("pg_restore:")
    ]
    try:
        mark = time.monotonic()
        # Interpolated table names, from a module constant that is never caller-supplied.
        # Parameters cannot name a relation, so the alternative to a literal here is a
        # catalogue lookup that would still end in an interpolated identifier.
        engine = create_engine(scratch_url)
        with Session(engine) as session:
            tables = {
                name: int(session.execute(text(f"SELECT count(*) FROM {name}")).scalar_one())
                for name in RESTORE_CHECK_TABLES
            }
        engine.dispose()
        timings["count_seconds"] = round(time.monotonic() - mark, 2)

        mark = time.monotonic()
        ledger = verify(dsn=scratch_url)
        timings["verify_seconds"] = round(time.monotonic() - mark, 2)
    finally:
        if not keep:
            _psql_admin(admin_url, f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')

    timings["total_seconds"] = round(time.monotonic() - started, 2)
    return {
        "job": "restore",
        "bucket": bucket,
        "key": key,
        "version_id": version_id,
        "sha256": sha256,
        "scratch_database": scratch,
        "kept": keep,
        "pg_restore_exit": completed.returncode,
        "pg_restore_warnings": warnings[:5],
        "rows": tables,
        "ledger": {
            "tenants": ledger["tenants"],
            "entries": ledger["entries"],
            "ok": ledger["ok"],
            "broken": ledger["broken"],
        },
        "timings": timings,
        # Both halves have to hold. A restore that returns rows with a broken chain is a
        # worse outcome than one that fails outright, because it looks like a recovery.
        "ok": bool(ledger["ok"] and all(count > 0 for count in tables.values())),
    }


def _emit(report: dict[str, Any]) -> None:
    """One JSON object per line, on stdout.

    Not the logging module: this is the artefact, not a trace of producing it, and it has
    to survive being piped into something that expects one document per line.
    """
    print(json.dumps(report, default=str), flush=True)


def schedule(
    *,
    bucket: str,
    retention_days: int,
    backup_interval: float,
    verify_interval: float,
    drill_interval: float = DEFAULT_DRILL_INTERVAL,
    dsn: str | None = None,
) -> int:
    """Run both jobs on their own clocks until killed.

    A loop rather than cron, for the same reason `services/agent/src/worker.py` polls: the
    deployment already has a supervisor that restarts containers, and a second scheduler
    inside the image is a second thing that can be silently not running. `docker compose
    ps` answering "up" is then the truth about whether backups are happening.

    A failure is logged and the loop continues. A backup job that exits on the first error
    stops taking backups at the moment something is wrong with the system, which is when
    the next one matters most.

    The restore drill runs on this loop rather than as a thing somebody remembers to do,
    because that is the whole finding behind it. A backup path that is never exercised
    fails silently and keeps reporting success — which is exactly what happened between
    weeks 15 and 16, and what `restore`'s own first run found in its first thirty seconds.
    A drill that depends on being remembered is a drill that stops after the incident it
    was added for.
    """
    next_backup = 0.0
    next_verify = 0.0
    # Not zero. The first drill waits a full interval, because it restores the backup this
    # loop is about to take and there is nothing in the bucket yet on a cold start.
    next_drill = time.monotonic() + drill_interval
    while True:
        now = time.monotonic()
        if now >= next_backup:
            try:
                _emit(backup(bucket=bucket, retention_days=retention_days, dsn=dsn))
            except RetentionError as exc:
                _emit({"job": "backup", "ok": False, "error": str(exc)})
            next_backup = time.monotonic() + backup_interval
        if now >= next_verify:
            try:
                _emit(verify(dsn=dsn))
            except Exception as exc:  # the loop has to outlive any one failure
                _emit({"job": "verify", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            next_verify = time.monotonic() + verify_interval
        if now >= next_drill:
            try:
                _emit(restore(bucket=bucket, dsn=dsn))
            except Exception as exc:
                _emit({"job": "restore", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            next_drill = time.monotonic() + drill_interval
        due = min(next_backup, next_verify, next_drill)
        time.sleep(min(60.0, max(1.0, due - time.monotonic())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=("backup", "verify", "catalogue", "restore", "schedule"))
    parser.add_argument(
        "--bucket",
        default=os.environ.get("DRAWBRIDGE_S3_BUCKET_BACKUPS", "drawbridge-backups"),
        help="must have been created with object lock enabled",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=int(os.environ.get("DRAWBRIDGE_BACKUP_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)),
    )
    parser.add_argument("--backup-interval", type=float, default=DEFAULT_BACKUP_INTERVAL)
    parser.add_argument("--verify-interval", type=float, default=DEFAULT_VERIFY_INTERVAL)
    parser.add_argument(
        "--drill-interval",
        type=float,
        default=float(os.environ.get("DRAWBRIDGE_BACKUP_DRILL_INTERVAL", DEFAULT_DRILL_INTERVAL)),
    )
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--key", default=None, help="restore: object key; default newest")
    parser.add_argument("--version-id", default=None, help="restore: a specific version")
    parser.add_argument(
        "--scratch",
        default="drawbridge_restore_drill",
        help="restore: database to create and restore into; never the source",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="restore: leave the scratch database in place to inspect",
    )
    parser.add_argument(
        "--drill",
        action="store_true",
        help="restore: accepted and ignored; the verb is already a drill",
    )
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")

    try:
        if args.job == "backup":
            _emit(backup(bucket=args.bucket, retention_days=args.retention_days, dsn=args.dsn))
            return 0
        if args.job == "catalogue":
            report = catalogue(bucket=args.bucket)
            _emit(report)
            return 0 if report["ok"] else 1
        if args.job == "verify":
            report = verify(dsn=args.dsn)
            _emit(report)
            # Non-zero on a break, so whatever runs this notices without parsing JSON.
            return 0 if report["ok"] else 1
        if args.job == "restore":
            report = restore(
                bucket=args.bucket,
                dsn=args.dsn,
                key=args.key,
                version_id=args.version_id,
                scratch=args.scratch,
                keep=args.keep,
            )
            _emit(report)
            return 0 if report["ok"] else 1
        return schedule(
            bucket=args.bucket,
            retention_days=args.retention_days,
            backup_interval=args.backup_interval,
            verify_interval=args.verify_interval,
            drill_interval=args.drill_interval,
            dsn=args.dsn,
        )
    except RetentionError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
