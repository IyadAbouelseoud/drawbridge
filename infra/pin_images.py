"""Resolve every third-party image tag in the on-prem stack to a digest, and rewrite it.

    python infra/pin_images.py --check      # CI: fail if any image is unpinned or drifted
    python infra/pin_images.py              # rewrite docker-compose.onprem.yml in place

A tag is a name its publisher can repoint. `n8nio/n8n:1.70.1` today and `n8nio/n8n:1.70.1`
in eighteen months are the same string and need not be the same bytes, so a stack that
re-pulls on restart is a stack whose contents can change without a commit. A digest is the
content, so `docker pull` either gets identical bytes or fails.

**What this does not give you.** A digest says the bytes have not changed since somebody
wrote the digest down. It says nothing about whether they were trustworthy then. That is
signature verification's job — cosign, or a registry admission policy — and it is not in
this repository. Recording the difference matters more than closing it here: a deployment
that believes digests are provenance will skip the control that actually is.

The tag is kept alongside the digest. Docker resolves the digest and ignores the tag, so
it is documentation and is not load-bearing — which is the point, because
`redis@sha256:ff02b5...` on its own tells a reviewer nothing, and a digest nobody can read
is a digest nobody checks.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

COMPOSE = pathlib.Path(__file__).resolve().parents[1] / "docker-compose.onprem.yml"

#: `image: <ref>[@<digest>]`, where <ref> is a third-party name. Images this repository
#: builds are excluded by the negative lookahead: they carry ${DRAWBRIDGE_VERSION} and are
#: produced by the same commit that deploys them, so a digest would pin the build to the
#: last time somebody ran this script rather than to the source.
_IMAGE = re.compile(
    r"^(?P<indent>\s*)image:\s*(?!drawbridge/)(?P<ref>[^\s@]+)(?:@(?P<digest>sha256:[0-9a-f]{64}))?\s*$",
    re.MULTILINE,
)


class PinError(RuntimeError):
    """A tag could not be resolved. The registry is the only thing that can answer."""


def resolve(ref: str) -> str:
    """The manifest-list digest for `ref`, read from the registry.

    `buildx imagetools inspect` rather than `docker image inspect`: the latter reports
    what happens to be in the local cache, which answers "what did I pull once" rather
    than "what does this tag mean now". A machine that has never pulled the image gives
    the right answer, which is the property a CI check needs.
    """
    try:
        out = subprocess.run(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                ref,
                "--format",
                "{{println .Manifest.Digest}}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"could not run docker to resolve {ref}: {type(exc).__name__}"
        raise PinError(msg) from exc
    digest = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
    if not digest.startswith("sha256:"):
        msg = f"registry did not return a digest for {ref}"
        raise PinError(msg)
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if any image is unpinned or has drifted",
    )
    parser.add_argument("--compose", type=pathlib.Path, default=COMPOSE)
    args = parser.parse_args(argv)

    text = args.compose.read_text(encoding="utf-8")
    matches = list(_IMAGE.finditer(text))
    if not matches:
        print(f"no third-party images in {args.compose}", file=sys.stderr)
        return 1

    seen: dict[str, str] = {}
    drift: list[str] = []
    for match in matches:
        ref = match["ref"]
        if ref not in seen:
            try:
                seen[ref] = resolve(ref)
            except PinError as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 1
        current = match["digest"]
        state = "pinned" if current == seen[ref] else ("drifted" if current else "unpinned")
        if state != "pinned":
            drift.append(f"  {state:<9} {ref}\n            {current or '(none)'} -> {seen[ref]}")
        print(f"  {state:<9} {ref}")

    if args.check:
        if drift:
            print("\n" + "\n".join(drift), file=sys.stderr)
            print(
                f"\n{len(drift)} image(s) are not pinned to the digest the registry now "
                "serves. Run `make pin-images` and review the diff — a moved digest on an "
                "unchanged tag is worth understanding before it is accepted.",
                file=sys.stderr,
            )
            return 1
        print(f"\nall {len(seen)} third-party images pinned and current")
        return 0

    def _rewrite(match: re.Match[str]) -> str:
        return f"{match['indent']}image: {match['ref']}@{seen[match['ref']]}"

    args.compose.write_text(_IMAGE.sub(_rewrite, text), encoding="utf-8")
    print(f"\nwrote {len(seen)} digest(s) to {args.compose}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
