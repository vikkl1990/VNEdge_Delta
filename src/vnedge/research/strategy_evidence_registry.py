"""Canonical, fail-closed strategy/evidence and route-cost authority.

The strategy registry is the only source allowed to publish a dashboard lane
or authorize a paper/production manifest.  Performance claims are read from a
hash-pinned evidence artifact; registry membership never substitutes for a
signed governance proof or the risk gateway.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REGISTRY = REPO_ROOT / "configs/strategy_registry.yaml"
DEFAULT_COST_CONTRACTS = REPO_ROOT / "configs/cost_contracts.yaml"
_BLOCKED_STATUSES = frozenset({"blocked", "disabled"})
_SNAPSHOT_CACHE_LOCK = threading.RLock()
_SNAPSHOT_CACHE: dict[
    str, tuple[tuple[tuple[str, int, int, int, int, int], ...], dict[str, Any]]
] = {}


@dataclass(frozen=True)
class RouteCostContract:
    contract_id: str
    description: str
    route: str
    entry_liquidity: str
    exit_liquidity: str
    base_entry_fee_bps: float
    base_exit_fee_bps: float
    gst_bps: float
    slippage_bps: float
    safety_buffer_bps: float
    total_roundtrip_bps: float
    maker_fee_bps: float
    taker_fee_bps: float
    slippage_bps_per_leg: float
    includes_gst: bool
    calibration_status: str

    @property
    def round_trip_cost_bps(self) -> float:
        return self.total_roundtrip_bps

    def paper_cost_model(self) -> dict[str, Any]:
        return {
            "maker_fee_bps": self.maker_fee_bps,
            "taker_fee_bps": self.taker_fee_bps,
            "slippage_bps_per_leg": self.slippage_bps_per_leg,
            "round_trip_bps": self.total_roundtrip_bps,
            "includes_gst": self.includes_gst,
            "cost_contract": self.contract_id,
        }

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        # Compatibility aliases are presentation-only; the canonical YAML
        # retains one unambiguous field for every component.
        value.update(
            {
                "label": self.description,
                "entry_route": self.entry_liquidity,
                "exit_route": self.exit_liquidity,
                "fee_bps": self.base_entry_fee_bps
                + self.base_exit_fee_bps
                + self.gst_bps,
                "slippage_reserve_bps": self.slippage_bps
                + self.safety_buffer_bps,
                "round_trip_cost_bps": self.total_roundtrip_bps,
            }
        )
        return value


def load_registry(path: str | Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    registry_path = Path(path)
    raw = _read_yaml(registry_path, "strategy registry")
    if raw.get("schema_version") != "vnedge.strategy_registry.v1":
        raise ValueError("unsupported canonical strategy registry schema")
    if not isinstance(raw.get("strategies"), dict):
        raise TypeError("strategy registry strategies must be an object")
    if not str(raw.get("cost_contracts_file") or "").strip():
        raise ValueError("strategy registry must declare cost_contracts_file")
    return raw


def load_cost_contracts(path: str | Path = DEFAULT_COST_CONTRACTS) -> dict[str, Any]:
    contracts_path = Path(path)
    raw = _read_yaml(contracts_path, "cost contracts")
    if raw.get("schema_version") != "vnedge.cost_contracts.v1":
        raise ValueError("unsupported cost-contract schema")
    if not isinstance(raw.get("cost_contracts"), dict):
        raise TypeError("cost_contracts must be an object")
    return raw


def registry_cost_contracts_path(registry_path: str | Path = DEFAULT_REGISTRY) -> Path:
    path = Path(registry_path)
    raw = load_registry(path)
    return _resolve_locator(path, str(raw["cost_contracts_file"]))


def route_cost_contract(
    contract_id: str,
    *,
    registry_path: str | Path = DEFAULT_REGISTRY,
    cost_contracts_path: str | Path | None = None,
) -> RouteCostContract:
    path = (
        Path(cost_contracts_path)
        if cost_contracts_path is not None
        else registry_cost_contracts_path(registry_path)
    )
    value = load_cost_contracts(path)["cost_contracts"].get(contract_id)
    if not isinstance(value, dict):
        raise KeyError(f"unknown route cost contract: {contract_id}")
    contract = RouteCostContract(
        contract_id=contract_id,
        description=str(value.get("description") or ""),
        route=str(value.get("route") or ""),
        entry_liquidity=str(value.get("entry_liquidity") or ""),
        exit_liquidity=str(value.get("exit_liquidity") or ""),
        base_entry_fee_bps=float(value.get("base_entry_fee_bps")),
        base_exit_fee_bps=float(value.get("base_exit_fee_bps")),
        gst_bps=float(value.get("gst_bps")),
        slippage_bps=float(value.get("slippage_bps")),
        safety_buffer_bps=float(value.get("safety_buffer_bps", 0.0)),
        total_roundtrip_bps=float(value.get("total_roundtrip_bps")),
        maker_fee_bps=float(value.get("maker_fee_bps")),
        taker_fee_bps=float(value.get("taker_fee_bps")),
        slippage_bps_per_leg=float(value.get("slippage_bps_per_leg")),
        includes_gst=value.get("includes_gst") is True,
        calibration_status=str(value.get("calibration_status") or ""),
    )
    if not contract.description or contract.entry_liquidity not in {"maker", "taker"}:
        raise ValueError(f"invalid route cost contract: {contract_id}")
    if contract.exit_liquidity not in {"maker", "taker"}:
        raise ValueError(f"invalid route cost contract: {contract_id}")
    component_total = (
        contract.base_entry_fee_bps
        + contract.base_exit_fee_bps
        + contract.gst_bps
        + contract.slippage_bps
        + contract.safety_buffer_bps
    )
    if abs(component_total - contract.total_roundtrip_bps) > 1e-9:
        raise ValueError(f"route cost contract does not add up: {contract_id}")
    if not contract.includes_gst:
        raise ValueError(f"route cost contract must explicitly include GST: {contract_id}")
    return contract


def build_registry_snapshot(path: str | Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    registry_path = Path(path)
    raw = load_registry(registry_path)
    contracts_path = registry_cost_contracts_path(registry_path)
    strategies: dict[str, Any] = {}
    invalid: list[dict[str, str]] = []
    for strategy_id, value in raw["strategies"].items():
        if not isinstance(value, dict):
            invalid.append({"strategy_id": str(strategy_id), "reason": "entry is not an object"})
            continue
        result = _verify_strategy(
            str(strategy_id),
            value,
            registry_path=registry_path,
            cost_contracts_path=contracts_path,
        )
        strategies[str(strategy_id)] = result
        if result["evidence"]["verified"] is not True:
            invalid.append(
                {"strategy_id": str(strategy_id), "reason": result["evidence"]["reason"]}
            )
    contracts_raw = load_cost_contracts(contracts_path)["cost_contracts"]
    contracts = {
        contract_id: route_cost_contract(
            contract_id,
            registry_path=registry_path,
            cost_contracts_path=contracts_path,
        ).to_dict()
        for contract_id in contracts_raw
    }
    global_authority = raw.get("global_authority")
    global_authority = global_authority if isinstance(global_authority, dict) else {}
    return {
        "schema_version": raw["schema_version"],
        "version": raw.get("version"),
        "updated": raw.get("updated"),
        "registry_id": raw.get("registry_id"),
        "registry_path": str(registry_path.resolve()),
        "cost_contracts_path": str(contracts_path.resolve()),
        "strategies": strategies,
        "route_cost_contracts": contracts,
        "invalid_evidence": invalid,
        "all_displayed_evidence_verified": not any(
            item.get("display") is True and item["evidence"]["verified"] is not True
            for item in strategies.values()
        ),
        "authority": {
            "paper_trials_enabled": global_authority.get("paper_trials_enabled") is True,
            "can_trade": global_authority.get("can_trade") is True,
            "can_promote": global_authority.get("can_promote") is True,
            "live_orders_enabled": global_authority.get("live_orders_enabled") is True,
            "order_route": str(global_authority.get("order_route") or "absent"),
        },
    }


def build_registry_snapshot_cached(
    path: str | Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    """Reuse a verified snapshot until any registry dependency changes.

    The cache key includes device, inode, size, mtime and ctime for the
    registry, cost contracts and every evidence artifact. Unlike a TTL cache,
    it cannot keep serving an artifact after an ordinary local rewrite. A deep
    copy prevents dashboard presentation code from mutating cached authority.
    """

    registry_path = Path(path).resolve()
    fingerprint = _registry_dependency_fingerprint(registry_path)
    cache_key = str(registry_path)
    with _SNAPSHOT_CACHE_LOCK:
        cached = _SNAPSHOT_CACHE.get(cache_key)
        if cached is not None and cached[0] == fingerprint:
            return copy.deepcopy(cached[1])
    snapshot = build_registry_snapshot(registry_path)
    with _SNAPSHOT_CACHE_LOCK:
        _SNAPSHOT_CACHE[cache_key] = (fingerprint, copy.deepcopy(snapshot))
    return snapshot


def _registry_dependency_fingerprint(
    registry_path: Path,
) -> tuple[tuple[str, int, int, int, int, int], ...]:
    raw = load_registry(registry_path)
    paths = [registry_path, registry_cost_contracts_path(registry_path)]
    for value in raw["strategies"].values():
        if not isinstance(value, dict):
            continue
        evidence = value.get("evidence")
        if not isinstance(evidence, dict):
            continue
        locator = str(evidence.get("primary") or "")
        if locator:
            paths.append(_resolve_locator(registry_path, locator))

    fingerprint: list[tuple[str, int, int, int, int, int]] = []
    for dependency in sorted({item.resolve() for item in paths}, key=str):
        try:
            stat = dependency.stat()
            fingerprint.append(
                (
                    str(dependency),
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                )
            )
        except OSError:
            fingerprint.append((str(dependency), 0, 0, -1, 0, 0))
    return tuple(fingerprint)


def strategy_authority_blockers(
    strategy_id: str,
    *,
    purpose: str,
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> tuple[str, ...]:
    """Validate registry authority without replacing signed proof validation."""

    if purpose not in {"observation", "paper", "live"}:
        return (f"unsupported strategy registry purpose: {purpose}",)
    snapshot = build_registry_snapshot(registry_path)
    entry = snapshot["strategies"].get(strategy_id)
    if not isinstance(entry, dict):
        return (f"strategy is absent from canonical registry: {strategy_id}",)
    failures: list[str] = []
    if entry["evidence"].get("verified") is not True:
        failures.append("strategy evidence is not hash verified")
    status = str(entry.get("status") or "")
    if status in _BLOCKED_STATUSES:
        failures.append(f"strategy status {status} is absolute")
    allowed_status = {
        "observation": {"research", "shadow"},
        "paper": {"paper"},
        "live": {"live"},
    }[purpose]
    if status not in allowed_status:
        failures.append(f"strategy status {status or 'missing'} does not authorize {purpose}")
    if entry["authority"].get(purpose) is not True:
        failures.append(f"strategy registry authority.{purpose}=true is required")
    global_authority = snapshot["authority"]
    if purpose == "paper" and global_authority["paper_trials_enabled"] is not True:
        failures.append("canonical registry globally blocks paper trials")
    if purpose == "live":
        if global_authority["can_trade"] is not True:
            failures.append("canonical registry globally blocks trading")
        if global_authority["live_orders_enabled"] is not True:
            failures.append("canonical registry globally blocks live orders")
        if global_authority["order_route"] == "absent":
            failures.append("canonical registry has no live order route")
    return tuple(failures)


def attach_verified_lane_evidence(
    rows: list[dict[str, Any]],
    *,
    registry_path: str | Path = DEFAULT_REGISTRY,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    """Publish only registered lanes backed by present, hash-verified evidence."""

    registry = build_registry_snapshot_cached(registry_path)
    accepted: list[dict[str, Any]] = []
    withheld: list[dict[str, str]] = []
    for row in rows:
        strategy_id = str(row.get("strategy_id") or "")
        entry = registry["strategies"].get(strategy_id)
        if not isinstance(entry, dict):
            withheld.append({"strategy_id": strategy_id, "reason": "not_registered"})
            continue
        if entry.get("display") is not True:
            withheld.append({"strategy_id": strategy_id, "reason": "display_not_authorized"})
            continue
        evidence = entry["evidence"]
        if evidence.get("verified") is not True:
            withheld.append({"strategy_id": strategy_id, "reason": evidence["reason"]})
            continue
        enriched = dict(row)
        enriched.update(
            {
                "registry_verified": True,
                "evidence_artifact": evidence["primary"],
                "evidence_sha256": evidence["actual_sha256"],
                "evidence_metrics": copy.deepcopy(evidence["metrics"]),
                "evidence_warnings": list(evidence["warning_badges"]),
                "hypothesis_class": entry.get("hypothesis_class"),
                "trade_horizon": entry.get("type"),
                "lifecycle": entry.get("status"),
                "edge_claim": entry.get("edge_claim"),
                "route_cost_contract": entry["route_cost_contract"],
                "can_trade": False,
                "can_promote": False,
            }
        )
        accepted.append(enriched)
    return accepted, withheld, registry


_HEURISTIC_EDGE_NAMES = {
    "expected_edge_bps": "heuristic_projected_gross_bps",
    "expected_net_bps": "heuristic_projected_net_bps",
    "expected_net_edge_bps": "structural_net_headroom_bps",
    "average_expected_net_bps": "average_heuristic_projected_net_bps",
    "minimum_expected_net_bps": "minimum_heuristic_projected_net_bps",
}


def relabel_uncalibrated_edge_fields(value: Any) -> Any:
    """Rename dashboard-facing heuristic claims; empirical outcomes are untouched."""

    if isinstance(value, list):
        return [relabel_uncalibrated_edge_fields(item) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    out: dict[str, Any] = {}
    for key, item in value.items():
        renamed = _HEURISTIC_EDGE_NAMES.get(key, key)
        for old, new in (
            ("expected_net_edge_bps_", "structural_net_headroom_bps_"),
            ("expected_edge_bps_", "heuristic_projected_gross_bps_"),
            ("algo_expected_net_edge_bps_", "algo_structural_net_headroom_bps_"),
        ):
            if renamed.startswith(old):
                renamed = new + renamed[len(old) :]
                break
        out[renamed] = relabel_uncalibrated_edge_fields(item)
    return out


def dashboard_metric_semantics() -> dict[str, str]:
    return {
        "structural_net_headroom_bps": (
            "mechanical target distance minus the named route-cost contract; "
            "not calibrated expectancy or realized edge"
        ),
        "heuristic_projected_gross_bps": (
            "uncalibrated rule projection; not an empirical expectancy or trade promise"
        ),
        "heuristic_projected_net_bps": (
            "legacy uncalibrated projection minus named costs; not realized edge"
        ),
        "average_net_bps": "empirical after-cost outcome from the linked evidence artifact",
    }


def _verify_strategy(
    strategy_id: str,
    value: Mapping[str, Any],
    *,
    registry_path: Path,
    cost_contracts_path: Path,
) -> dict[str, Any]:
    reasons: list[str] = []
    if str(value.get("id") or "") != strategy_id:
        reasons.append("strategy_id_mismatch")
    evidence_spec = value.get("evidence")
    evidence_spec = evidence_spec if isinstance(evidence_spec, dict) else {}
    artifact_name = str(evidence_spec.get("primary") or "")
    expected = str(evidence_spec.get("hash") or "").lower()
    if expected.startswith("sha256:"):
        expected = expected.removeprefix("sha256:")
    artifact = _resolve_locator(registry_path, artifact_name)
    actual = _sha256(artifact) if artifact.is_file() else None
    payload = _read_evidence(artifact)
    missing = [
        field
        for field in evidence_spec.get("required_fields", [])
        if _value_at_path(payload, str(field), missing=_MISSING) is _MISSING
    ]
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        reasons.append("invalid_expected_sha256")
    if actual is None:
        reasons.append("artifact_missing")
    elif actual != expected:
        reasons.append("sha256_mismatch")
    if missing:
        reasons.append("missing_required_fields:" + ",".join(map(str, missing)))
    if payload.get("can_trade") is True or payload.get("can_promote") is True:
        reasons.append("evidence_claims_forbidden_authority")

    metrics: dict[str, Any] = {}
    metric_specs = evidence_spec.get("metrics")
    metric_specs = metric_specs if isinstance(metric_specs, dict) else {}
    for name, spec in metric_specs.items():
        if not isinstance(spec, dict) or not str(spec.get("path") or ""):
            reasons.append(f"invalid_metric_spec:{name}")
            continue
        observed = _value_at_path(payload, str(spec["path"]), missing=_MISSING)
        if observed is _MISSING:
            reasons.append(f"missing_metric:{name}")
            continue
        metrics[str(name)] = observed
        if "expected" in spec and not _metric_equal(observed, spec["expected"]):
            reasons.append(f"metric_snapshot_mismatch:{name}")

    contract_id = str(value.get("cost_contract") or "")
    try:
        contract = route_cost_contract(
            contract_id,
            registry_path=registry_path,
            cost_contracts_path=cost_contracts_path,
        ).to_dict()
    except (KeyError, TypeError, ValueError):
        contract = None
        reasons.append("invalid_cost_contract")

    status = str(value.get("status") or "")
    authority = value.get("authority") if isinstance(value.get("authority"), dict) else {}
    normalized_authority = {
        "observation": authority.get("observation") is True,
        "paper": authority.get("paper") is True,
        "live": authority.get("live") is True,
        "can_trade": False,
        "can_promote": False,
    }
    if status in _BLOCKED_STATUSES and any(normalized_authority.values()):
        reasons.append("blocked_or_disabled_status_has_authority")
        normalized_authority.update({"observation": False, "paper": False, "live": False})

    n_trades = metrics.get("n_trades")
    required_n = evidence_spec.get("required_n")
    warnings: list[str] = []
    if evidence_spec.get("single_window") is True:
        warnings.append("SINGLE_WINDOW")
    if (
        isinstance(n_trades, (int, float))
        and isinstance(required_n, (int, float))
        and n_trades < required_n
    ):
        warnings.append("SAMPLE_BELOW_REQUIRED")
    if evidence_spec.get("sealed_holdout") is True:
        warnings.append("SEALED_HOLDOUT_UNOPENED")

    result = dict(value)
    result["strategy_id"] = strategy_id
    result["trade_horizon"] = value.get("type")
    result["lifecycle"] = status
    result["route_cost_contract"] = contract
    result["authority"] = normalized_authority
    result["evidence"] = {
        "primary": artifact_name,
        "artifact": artifact_name,
        "resolved_artifact": str(artifact),
        "expected_sha256": expected,
        "actual_sha256": actual,
        "required_fields": list(evidence_spec.get("required_fields", [])),
        "metrics": metrics,
        "required_n": required_n,
        "sealed_holdout": evidence_spec.get("sealed_holdout") is True,
        "single_window": evidence_spec.get("single_window") is True,
        "warning_badges": warnings,
        "verified": not reasons,
        "reason": "verified" if not reasons else ";".join(reasons),
    }
    return result


_MISSING = object()


def _read_yaml(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"unable to load {label}: {path}") from exc
    if not isinstance(raw, dict):
        raise TypeError(f"{label} must be an object")
    return raw


def _resolve_locator(anchor: Path, locator: str) -> Path:
    candidate = Path(locator)
    if candidate.is_absolute():
        return candidate
    repo_candidate = REPO_ROOT / candidate
    if repo_candidate.exists() or locator.startswith(("configs/", "research/", "data/")):
        return repo_candidate
    return anchor.resolve().parent / candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_evidence(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text()
        value = (
            yaml.safe_load(text)
            if path.suffix.lower() in {".yaml", ".yml"}
            else json.loads(text)
        )
    except (OSError, json.JSONDecodeError, yaml.YAMLError):
        return {}
    return value if isinstance(value, dict) else {}


def _value_at_path(payload: Mapping[str, Any], dotted: str, *, missing: Any) -> Any:
    current: Any = payload
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return missing
        current = current[part]
    return current


def _metric_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return abs(float(actual) - float(expected)) <= 1e-9
    return actual == expected
