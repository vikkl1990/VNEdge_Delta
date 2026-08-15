"""Fail-closed release validation for the Delta production image.

This checker grants no runtime authority. It only prevents a release from
being assembled from a dirty tree, an unpinned dependency set, an untraceable
commit, or a production manifest whose safety defaults have drifted open.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from vnedge.research.strategy_evidence_registry import build_registry_snapshot

_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED_LOCKED = frozenset(
    {
        "cryptography",
        "delta-rest-client",
        "fastapi",
        "numpy",
        "pandas",
        "pydantic",
        "pydantic-settings",
        "pyarrow",
        "pyyaml",
        "uvicorn",
        "websockets",
        "zstandard",
    }
)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class ReleaseCheck:
    passed: bool
    blockers: tuple[str, ...]
    commit: str | None
    locked_packages: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "vnedge.production_release_check.v1",
            "passed": self.passed,
            "blockers": list(self.blockers),
            "commit": self.commit,
            "locked_packages": self.locked_packages,
            "can_trade": False,
            "can_promote": False,
        }


def validate_lock(path: Path) -> tuple[int, list[str]]:
    blockers: list[str] = []
    packages: dict[str, str] = {}
    if not path.is_file():
        return 0, ["production dependency lock is missing"]
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.fullmatch(line)
        if match is None:
            blockers.append(f"unlocked requirement at line {number}: {line}")
            continue
        name = _canonical(match.group(1))
        if name in packages:
            blockers.append(f"duplicate locked package: {name}")
        packages[name] = match.group(2)
    missing = sorted(_REQUIRED_LOCKED - packages.keys())
    if missing:
        blockers.append("required production packages are not locked: " + ", ".join(missing))
    return len(packages), blockers


def validate_runtime_config(path: Path) -> list[str]:
    if not path.is_file():
        return ["production runtime config is missing"]
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [f"production runtime config is unreadable: {exc}"]
    if not isinstance(payload, dict):
        return ["production runtime config must be a mapping"]
    blockers: list[str] = []
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    promotion = (
        payload.get("promotion") if isinstance(payload.get("promotion"), dict) else {}
    )
    scanner_authority = (
        payload.get("scanner_authority")
        if isinstance(payload.get("scanner_authority"), dict)
        else {}
    )
    if payload.get("venue") != "delta_india":
        blockers.append("production venue must be delta_india")
    if runtime.get("default_stage") != "research":
        blockers.append("production default stage must remain research")
    if runtime.get("live_orders_enabled") is not False:
        blockers.append("live_orders_enabled must default false")
    if scanner_authority.get("enabled_for_paper") is not False:
        blockers.append("scanner authority must default paper disabled")
    if scanner_authority.get("enabled_for_live") is not False:
        blockers.append("scanner authority must default live disabled")
    if scanner_authority.get("scanner_id") == "mtf_amf_directional_rejection_v3":
        if scanner_authority.get("trade_horizon") != "swing":
            blockers.append("AMF v3 must be labelled as a swing hypothesis")
        if scanner_authority.get("edge_claim") != "unproven_swing_hypothesis_not_scalping_edge":
            blockers.append("AMF v3 must not claim scalping edge")
    for key in (
        "signed_paper_eligibility_required",
        "signed_stage_authorization_required",
        "single_use_nonce_required",
    ):
        if promotion.get(key) is not True:
            blockers.append(f"promotion.{key} must be true")
    return blockers


def validate_canonical_registry(repo_root: Path, runtime_path: Path) -> list[str]:
    try:
        runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
        scanner = runtime["scanner_authority"]
        registry_locator = str(scanner["strategy_registry"])
        registry_path = Path(registry_locator)
        if not registry_path.is_absolute():
            registry_path = repo_root / registry_path
        registry = build_registry_snapshot(registry_path)
        entry = registry["strategies"][str(scanner["scanner_id"])]
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        return [f"canonical strategy registry validation failed: {exc}"]
    blockers: list[str] = []
    if entry["evidence"].get("verified") is not True:
        blockers.append("production scanner evidence is not hash verified")
    contract_id = (entry.get("route_cost_contract") or {}).get("contract_id")
    if scanner.get("cost_contract") != contract_id:
        blockers.append("production scanner cost contract differs from canonical registry")
    if entry.get("type") != scanner.get("trade_horizon"):
        blockers.append("production scanner horizon differs from canonical registry")
    if entry.get("edge_claim") != scanner.get("edge_claim"):
        blockers.append("production scanner edge claim differs from canonical registry")
    return blockers


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=repo_root, text=True, stderr=subprocess.DEVNULL
    ).strip()


def evaluate_release(
    repo_root: Path,
    *,
    expected_sha: str | None = None,
    allow_dirty: bool = False,
) -> ReleaseCheck:
    blockers: list[str] = []
    commit: str | None = None
    try:
        commit = _git(repo_root, "rev-parse", "HEAD")
        dirty = _git(repo_root, "status", "--porcelain")
    except (OSError, subprocess.CalledProcessError):
        blockers.append("release root is not a readable git worktree")
        dirty = ""
    if commit is not None and _SHA.fullmatch(commit) is None:
        blockers.append("source commit is not a full SHA-1")
    if dirty and not allow_dirty:
        blockers.append("working tree is dirty; production releases require committed inputs")
    if expected_sha:
        expected = expected_sha.strip().lower()
        if _SHA.fullmatch(expected) is None:
            blockers.append("expected build SHA is not a full SHA-1")
        elif commit != expected:
            blockers.append("expected build SHA does not match checked-out commit")

    count, lock_blockers = validate_lock(repo_root / "requirements-production.lock")
    blockers.extend(lock_blockers)
    runtime_path = repo_root / "configs" / "production_live.yaml"
    blockers.extend(validate_runtime_config(runtime_path))
    blockers.extend(validate_canonical_registry(repo_root, runtime_path))
    return ReleaseCheck(not blockers, tuple(blockers), commit, count)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-sha")
    parser.add_argument("--allow-dirty", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = evaluate_release(
        args.repo_root.resolve(),
        expected_sha=args.expected_sha,
        allow_dirty=args.allow_dirty,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
