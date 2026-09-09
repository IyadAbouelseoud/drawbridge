"""Backups and ledger verification — the two jobs a four-year obligation actually needs.

    python scripts/retention.py backup            # one dump, uploaded under object lock
    python scripts/retention.py verify            # recompute every tenant's hash chain
    python scripts/retention.py catalogue         # every backup version, incl. masked ones
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
    """
    next_backup = 0.0
    next_verify = 0.0
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
        time.sleep(min(60.0, max(1.0, min(next_backup, next_verify) - time.monotonic())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=("backup", "verify", "catalogue", "schedule"))
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
    parser.add_argument("--dsn", default=None)
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
        return schedule(
            bucket=args.bucket,
            retention_days=args.retention_days,
            backup_interval=args.backup_interval,
            verify_interval=args.verify_interval,
            dsn=args.dsn,
        )
    except RetentionError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
