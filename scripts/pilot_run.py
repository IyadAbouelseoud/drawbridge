"""Push the seeded pilot corpora through the deployed pipeline, and assert the outcome.

    python scripts/pilot_run.py --all
    python scripts/pilot_run.py --lane us
    python scripts/pilot_run.py --lane ksa-time-barred

Week 12 built two corpora and a payload writer and stopped there. This is the part that
makes them a pilot rather than a fixture: each corpus goes through the real API, over the
real transport, with a real bearer token, and the script says up front what the answer has
to be.

**The two lanes prove opposite things, on purpose.**

The US corpus is a working backward-looking claim: 2021 entries, exported in 2024, still
inside §1313(r)'s three-year filing window in 2026. It has to reach a rendered CBP 7551.
That is the demonstration a customer buys.

The KSA time-barred corpus is duty paid more than three years ago. GCC Common Customs Law
Art. 174 bars it absolutely, and the assertion is that the pipeline **refuses** — that the
refund is zero and the reason names the article. A rules engine that cannot be shown
saying no is a rules engine nobody should believe when it says yes, and "we checked the
limitation period" is worth exactly as much as the refusal you can reproduce on demand.

**No documents, and that is stated rather than hidden.** The corpora carry provenance
boxes stamped `pilot-fixture` that trace to no PDF, so extraction is not exercised here —
there is nothing to extract from. What runs is classification onward: match, triage,
quantify, package. `scripts/e2e_pipeline_test.py` is the run that covers intake, against
generated documents that do exist in MinIO.

Nothing this script produces may be filed. Every figure descends from a fixture and the
packet says so on its face.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import text

from drawbridge_schemas.jurisdiction import profile_for
from scripts import pilot_ksa, pilot_us
from scripts.pilot_common import PilotCorpus, assert_not_evidence, owner_session, seed
from services.api.src.auth import SERVICE_SCOPE, mint
from services.api.src.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

TIMEOUT = httpx.Timeout(120.0, connect=10.0)
DEFAULT_BASE = os.environ.get("DRAWBRIDGE_API_URL", "http://localhost:8000")

# Art. 174 is an absolute bar, so the expected refund is exactly zero rather than
# approximately zero. A tolerance here would let a partially-applied limitation pass.
ZERO = Decimal("0.00")


@dataclass
class Report:
    """What happened, in order. Accumulated so a late failure still shows the early steps."""

    name: str
    expectation: str
    steps: list[tuple[str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def ok(self, step: str, detail: str = "") -> None:
        self.steps.append((step, detail))

    def check(self, step: str, condition: bool, detail: str) -> bool:
        self.steps.append((step, detail))
        if not condition:
            self.failures.append(f"{step}: {detail}")
        return condition

    def fail(self, step: str, detail: str) -> None:
        self.steps.append((step, detail))
        self.failures.append(f"{step}: {detail}")

    @property
    def passed(self) -> bool:
        return not self.failures

    def render(self) -> str:
        lines = ["", "=" * 78, self.name, f"expects: {self.expectation}", "=" * 78]
        for step, detail in self.steps:
            mark = "!!" if any(step in f for f in self.failures) else "  "
            lines.append(f" {mark} {step:<32} {detail}")
        verdict = "PASSED" if self.passed else "FAILED: " + "; ".join(self.failures)
        lines.append(f" -> {verdict}")
        return "\n".join(lines)


class Api:
    """Thin client over the deployed API. Never raises on a status; the caller judges."""

    def __init__(self, base: str) -> None:
        token = os.environ.get("DRAWBRIDGE_SERVICE_TOKEN", "").strip()
        if not token:
            # The same shape n8n carries: one run, driving whichever tenant the trigger
            # names. Against a real Authentik there is no secret here to sign with and the
            # token has to arrive in the environment instead.
            token = mint(
                get_settings(),
                subject="pilot-run",
                scopes=(SERVICE_SCOPE,),
                ttl_seconds=1800,
            )
        self._client = httpx.Client(
            base_url=base.rstrip("/"),
            timeout=TIMEOUT,
            headers={"Authorization": f"Bearer {token}"},
        )

    def post(self, path: str, body: Any) -> tuple[int, Any]:
        response = self._client.post(path, json=body)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    def close(self) -> None:
        self._client.close()


def _payload(corpus: PilotCorpus, as_of: str) -> dict[str, Any]:
    return {
        "tenant_id": str(corpus.tenant.tenant_id),
        "jurisdiction": corpus.jurisdiction.value,
        "imports": [line.model_dump(mode="json") for line in corpus.imports],
        "exports": [line.model_dump(mode="json") for line in corpus.exports],
        "as_of": as_of,
    }


def _expected_refund(corpus: PilotCorpus) -> Decimal:
    """What the corpus says the matcher must produce, to the cent.

    `expected_base` is duty and fees at risk; the statutory rate turns it into a refund —
    99% under §1313(j)(1), 100% under GCC Art. 16 §6, which does not take a haircut. The
    rate is read from the jurisdiction profile rather than written here, so a corpus
    cannot quietly disagree with the rules engine about the one multiplier.
    """
    rate = profile_for(corpus.jurisdiction).refund_rate
    return (corpus.expected_base * rate).quantize(Decimal("0.01"))


def _seed(corpus: PilotCorpus, report: Report) -> bool:
    """Make sure the corpus is in the database before driving it through the API.

    Idempotent: `seed` refuses to reseed lines that have already been designated, so a
    re-run does not double-count and does not require a teardown.
    """
    try:
        with owner_session() as session:
            written = seed(corpus, session)
    except Exception as exc:
        report.fail("seed corpus", str(exc))
        return False
    report.ok(
        "seed corpus",
        f"{written['imports']} import line(s), {written['exports']} export line(s)",
    )
    return True


def _run_pipeline(
    api: Api, corpus: PilotCorpus, report: Report, as_of: str
) -> dict[str, Any] | None:
    """Classify, match, triage. The three stages that decide whether a claim exists."""
    payload = _payload(corpus, as_of)

    status, classified = api.post(
        "/classification/run",
        {
            "jurisdiction": payload["jurisdiction"],
            "lines": [
                {
                    "line_id": line["line_id"],
                    "description": line["description"],
                    "declared_code": line["hts"]["code"],
                }
                for line in payload["imports"]
            ],
        },
    )
    if status != 200:
        report.fail("classify", f"HTTP {status} {classified}")
        return None
    report.ok(
        "classify",
        f"corroborated={classified['corroborated']}/{len(classified['lines'])} "
        f"unsupported={len(classified['unsupported'])}",
    )

    status, match = api.post(
        "/matching/run",
        {
            "jurisdiction": payload["jurisdiction"],
            "imports": payload["imports"],
            "exports": payload["exports"],
            "as_of": payload["as_of"],
        },
    )
    if status != 200:
        report.fail("match", f"HTTP {status} {match}")
        return None
    report.ok(
        "match",
        f"status={match['status']} matches={len(match['matches'])} "
        f"rejections={len(match['rejections'])} refund={match['total_refund']} "
        f"{match['currency']}",
    )

    status, triage = api.post(
        "/triage/evaluate",
        {"match_result": match, "confidences": [], "as_of": payload["as_of"]},
    )
    if status != 200:
        report.fail("triage", f"HTTP {status} {triage}")
        return None
    reasons = sorted({item["reason"] for item in triage["items"]})
    report.ok("triage", f"requires_review={triage['requires_review']} reasons={reasons or '-'}")

    return {"payload": payload, "match": match, "triage": triage}


def run_us(api: Api, report: Report, as_of: str) -> None:
    """The working lane. A packaged CBP 7551 or the run has failed."""
    corpus = pilot_us.build_corpus()
    # Belt and braces: `seed` asserts this too, but the check is cheap and what it
    # prevents — a fabricated figure reaching a customs authority — is not recoverable.
    assert_not_evidence(corpus)
    if not _seed(corpus, report):
        return

    stages = _run_pipeline(api, corpus, report, as_of)
    if stages is None:
        return
    payload, match, triage = stages["payload"], stages["match"], stages["triage"]

    refund = Decimal(str(match["total_refund"]))
    expected = _expected_refund(corpus)
    # To the cent, not "greater than zero". A pilot that only asserts a non-zero refund
    # passes just as happily on a wrong number, and a wrong number here is a wrong figure
    # on a form filed with CBP.
    report.check(
        "refund reproduces to the cent",
        refund == expected,
        f"{refund} {match['currency']}, expected {expected} "
        f"(99% of {corpus.expected_base} at risk)",
    )

    status, claim = api.post(
        "/claims/persist",
        {
            "tenant_id": payload["tenant_id"],
            "jurisdiction": payload["jurisdiction"],
            "imports": payload["imports"],
            "exports": payload["exports"],
            "matches": match["matches"],
            "total_refund": match["total_refund"],
            "requires_review": triage["requires_review"],
        },
    )
    if status != 201:
        report.fail("persist claim", f"HTTP {status} {claim}")
        return
    claim_id = str(claim["claim_id"])
    report.ok(
        "persist claim",
        f"{claim_id[:8]} state={claim['state']} deadline={claim['filing_deadline']}",
    )

    if claim["state"] != "approved":
        status, moved = api.post(
            "/claims/transition",
            {
                "claim_id": claim_id,
                "to_state": "approved",
                "actor": "pilot",
                "reason": "pilot corpus: no exception was raised by triage",
            },
        )
        if status != 200:
            report.fail("approve", f"HTTP {status} {moved}")
            return
        report.ok("approve", "quantified -> approved")

    # No `claimant` in the body. That is the assertion: the packager addresses the filing
    # from `tenant_profiles`, which is what week 13 added and what makes a second tenant
    # possible without a code change.
    status, packet = api.post("/packaging/build", {"claim_id": claim_id, "include_artifacts": True})
    if status != 200:
        report.fail("build packet", f"HTTP {status} {packet}")
        return
    filenames = [artifact["filename"] for artifact in packet["artifacts"]]
    report.check(
        "CBP 7551 rendered",
        any("cbp7551" in name for name in filenames),
        f"{filenames} transmittable={packet['transmittable']} "
        f"open_citations={len(packet['open_citations'])}",
    )
    # Read out of the rendered PDF rather than the manifest, which carries no claimant.
    # The point being proven is that these characters reached the page CBP reads, from a
    # database row, with nothing about the tenant in the request body.
    rendered = base64.b64decode(packet["artifacts"][0]["content_base64"])
    printed = [
        token
        for token in (b"Northbridge Trading LLC", b"95-4417293", b"J7K", b"Long Beach")
        if token in rendered
    ]
    report.check(
        "profile printed on the 7551",
        len(printed) == 4,
        f"{len(printed)}/4 of name, EIN, filer code, city found in the PDF",
    )

    check_the_profile_is_load_bearing(api, report, claim_id)


def run_ksa(api: Api, report: Report, as_of: str, *, time_barred: bool) -> None:
    """The refusing lane, when `time_barred`. Zero is the correct answer and the point."""
    # The corpus is dated relative to the filing date, so the same value must reach both
    # the builder and the matcher. Passing it to one and not the other is how week 12
    # ended up asserting a refund that no filing date could produce.
    corpus = pilot_ksa.build_corpus(time_barred=time_barred, as_of=date.fromisoformat(as_of))
    if not _seed(corpus, report):
        return

    stages = _run_pipeline(api, corpus, report, as_of)
    if stages is None:
        return
    match = stages["match"]
    refund = Decimal(str(match["total_refund"]))

    if not time_barred:
        expected = _expected_refund(corpus)
        report.check(
            "refund reproduces to the cent",
            refund == expected,
            f"{refund} {match['currency']}, expected {expected} (Art. 16 §6 takes no haircut)",
        )
        return

    report.check(
        "refund is exactly zero",
        refund == ZERO,
        f"{refund} {match['currency']}",
    )
    report.check(
        "every line was rejected",
        not match["matches"] and bool(match["rejections"]),
        f"{len(match['matches'])} match(es), {len(match['rejections'])} rejection(s)",
    )

    # The reason has to name the statute. A refusal that says only "ineligible" cannot be
    # shown to a customer whose entries it just wrote off.
    reasons = " ".join(str(r) for r in match["rejections"]).lower()
    report.check(
        "the reason names Art. 174",
        "174" in reasons or "time" in reasons or "limitation" in reasons,
        f"{match['rejections'][:2] if match['rejections'] else '-'}",
    )


def check_the_profile_is_load_bearing(api: Api, report: Report, claim_id: str) -> None:
    """Prove the profile is doing the work, by taking it away.

    A build that succeeds because a default crept in somewhere is indistinguishable from
    one that read the profile — until a second tenant onboards and files under the first
    tenant's EIN. So the run also asserts the refusal: no profile, no packet, and a
    message that names the missing fields rather than a generic 500.
    """
    with owner_session() as session:
        session.execute(
            text(
                "UPDATE tenant_profiles SET ein = NULL WHERE ein IS NOT NULL AND tenant_id = "
                "(SELECT tenant_id FROM claims WHERE claim_id = :claim)"
            ),
            {"claim": claim_id},
        )
        session.commit()
    try:
        status, body = api.post("/packaging/build", {"claim_id": claim_id})
        report.check(
            "an incomplete profile is refused",
            status == 422 and "ein" in str(body),
            f"HTTP {status} {str(body)[:110]}",
        )
    finally:
        # Put it back. The corpus is meant to be re-runnable, and a seeder that leaves the
        # tenant unfileable would make the next run fail for the previous run's reason.
        with owner_session() as session:
            session.execute(
                text(
                    "UPDATE tenant_profiles SET ein = :ein WHERE tenant_id = "
                    "(SELECT tenant_id FROM claims WHERE claim_id = :claim)"
                ),
                {"ein": "954417293", "claim": claim_id},
            )
            session.commit()


def _lanes(selected: str) -> Iterator[str]:
    if selected == "all":
        yield from ("us", "ksa", "ksa-time-barred")
    else:
        yield selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the pilot corpora through the pipeline.")
    parser.add_argument(
        "--lane",
        choices=("us", "ksa", "ksa-time-barred", "all"),
        default="all",
        help="which corpus to run",
    )
    parser.add_argument("--all", action="store_true", help="alias for --lane all")
    parser.add_argument("--base", default=DEFAULT_BASE, help="API base URL")
    parser.add_argument(
        "--as-of",
        default="2026-09-08",
        help="the filing date every window is measured against",
    )
    args = parser.parse_args(argv)

    api = Api(args.base)
    reports: list[Report] = []
    try:
        for lane in _lanes("all" if args.all else args.lane):
            if lane == "us":
                report = Report(
                    "US pilot — Northbridge Trading LLC, 2021 entries",
                    "a rendered CBP 7551, addressed from the tenant profile",
                )
                run_us(api, report, args.as_of)
            elif lane == "ksa":
                report = Report(
                    "KSA pilot — Al-Rajhi Regional Logistics, inside Art. 174",
                    "a non-zero refund; one line short of the USD 5,000 gate",
                )
                run_ksa(api, report, args.as_of, time_barred=False)
            else:
                report = Report(
                    "KSA pilot — the same corpus dated 2021",
                    "a refund of exactly zero, refused under GCC Art. 174",
                )
                run_ksa(api, report, args.as_of, time_barred=True)
            reports.append(report)
            print(report.render())
    finally:
        api.close()

    print("")
    print("Nothing here may be filed: every figure traces to a pilot-fixture box and to")
    print("no source document. See scripts/pilot_common.assert_not_evidence.")

    failed = [r for r in reports if not r.passed]
    if failed:
        print(f"\n{len(failed)} of {len(reports)} lane(s) FAILED", file=sys.stderr)
        return 1
    print(f"\n{len(reports)} lane(s) passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
