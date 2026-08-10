"""Bitcoin network regime sensor.

Research-only producer for BTC-native context: node sync, mempool pressure,
and fee-market stress. It never reads wallet state, never signs/sends
transactions, and never emits trade permission.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

BITCOIN_REGIME_ID = "bitcoin_regime_v1"
DEFAULT_LATEST = Path("research/live_research/bitcoin_regime_latest.json")
DEFAULT_HISTORY = Path("research/live_research/bitcoin_regime_history.jsonl")

READ_ONLY_RPC_METHODS = frozenset({
    "getblockchaininfo",
    "getmempoolinfo",
    "estimatesmartfee",
    "getchaintips",
})

FORBIDDEN_RPC_TOKENS = (
    "wallet",
    "send",
    "sign",
    "import",
    "dump",
    "encrypt",
    "create",
    "load",
    "unload",
    "backup",
    "bumpfee",
    "fund",
    "generate",
)


class UnsafeBitcoinRpcMethod(ValueError):
    """Raised before any non-read-only Bitcoin RPC can be attempted."""


class BitcoinCoreRpcClient:
    """Tiny JSON-RPC client with a hard read-only allowlist."""

    def __init__(
        self,
        url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.url = url
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        _validate_rpc_method(method)
        auth = (
            (self.username, self.password)
            if self.username is not None and self.password is not None
            else None
        )
        http = _httpx()
        response = http.post(
            self.url,
            auth=auth,
            json={
                "jsonrpc": "1.0",
                "id": "vnedge-bitcoin-regime",
                "method": method,
                "params": params or [],
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"bitcoin rpc {method} failed: {payload['error']}")
        return payload.get("result")


class MempoolApiClient:
    """Read-only mempool.space-compatible API client."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get(self, path: str) -> Any:
        http = _httpx()
        response = http.get(
            f"{self.base_url}/{path.lstrip('/')}",
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()


def run_bitcoin_regime(
    *,
    bitcoin_core: BitcoinCoreRpcClient | None = None,
    mempool_api: MempoolApiClient | None = None,
    fixture: Mapping[str, Any] | None = None,
    history_records: Iterable[Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect Bitcoin context and return a research-only artifact."""
    errors: list[dict[str, str]] = []
    node: dict[str, Any] = {}
    mempool: dict[str, Any] = {}
    fees: dict[str, Any] = {}
    source_flags = {
        "fixture": fixture is not None,
        "bitcoin_core_rpc": False,
        "mempool_api": False,
    }

    if fixture:
        node.update(_dict(fixture.get("node")))
        mempool.update(_dict(fixture.get("mempool")))
        fees.update(_dict(fixture.get("fees")))
        source_flags["bitcoin_core_rpc"] = bool(node)
        source_flags["mempool_api"] = bool(mempool)

    if bitcoin_core is not None:
        try:
            core = collect_bitcoin_core(bitcoin_core)
            node.update(core["node"])
            mempool.update({k: v for k, v in core["mempool"].items() if v is not None})
            fees.update({k: v for k, v in core["fees"].items() if v is not None})
            source_flags["bitcoin_core_rpc"] = True
        except Exception as exc:  # noqa: BLE001 - artifact should survive partial source failure
            errors.append({"source": "bitcoin_core_rpc", "error": str(exc)})

    if mempool_api is not None:
        try:
            mp = collect_mempool_api(mempool_api)
            mempool.update({k: v for k, v in mp["mempool"].items() if v is not None})
            fees.update({k: v for k, v in mp["fees"].items() if v is not None})
            source_flags["mempool_api"] = True
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": "mempool_api", "error": str(exc)})

    return build_bitcoin_regime(
        node=node,
        mempool=mempool,
        fees=fees,
        source_flags=source_flags,
        errors=errors,
        history_records=tuple(history_records),
        now=now,
    )


def collect_bitcoin_core(client: BitcoinCoreRpcClient) -> dict[str, dict[str, Any]]:
    """Collect safe read-only Bitcoin Core RPC state."""
    chain = _dict(client.call("getblockchaininfo"))
    mempool_info = _dict(client.call("getmempoolinfo"))
    fee_1 = _dict(client.call("estimatesmartfee", [1]))
    fee_3 = _dict(client.call("estimatesmartfee", [3]))
    tips = client.call("getchaintips")
    tips_count = len(tips) if isinstance(tips, list) else 0
    stale_tips = sum(
        1 for tip in tips or []
        if isinstance(tip, Mapping) and str(tip.get("status", "")) != "active"
    )

    blocks = _num(chain.get("blocks"))
    headers = _num(chain.get("headers"))
    verification_progress = _num(chain.get("verificationprogress"))
    initial_block_download = bool(chain.get("initialblockdownload", False))
    synced = (
        blocks > 0
        and headers >= blocks
        and headers - blocks <= 1
        and verification_progress >= 0.999
        and not initial_block_download
    )

    mempool_min_fee = _btc_per_kvb_to_sat_vb(mempool_info.get("mempoolminfee"))
    incremental_relay_fee = _btc_per_kvb_to_sat_vb(mempool_info.get("incrementalrelayfee"))

    return {
        "node": {
            "chain": chain.get("chain"),
            "synced": synced,
            "blocks": int(blocks),
            "headers": int(headers),
            "verification_progress": verification_progress,
            "initial_block_download": initial_block_download,
            "best_block_hash": chain.get("bestblockhash"),
            "pruned": bool(chain.get("pruned", False)),
            "chainwork": chain.get("chainwork"),
            "tips_count": tips_count,
            "stale_tips": stale_tips,
        },
        "mempool": {
            "tx_count": _num(mempool_info.get("size")),
            "vsize_vb": _num(mempool_info.get("bytes")),
            "memory_usage_bytes": _num(mempool_info.get("usage")),
            "min_fee_sat_vb": mempool_min_fee,
            "incremental_relay_fee_sat_vb": incremental_relay_fee,
        },
        "fees": {
            "fastest_fee_sat_vb": _btc_per_kvb_to_sat_vb(fee_1.get("feerate")),
            "half_hour_fee_sat_vb": _btc_per_kvb_to_sat_vb(fee_3.get("feerate")),
        },
    }


def collect_mempool_api(client: MempoolApiClient) -> dict[str, dict[str, Any]]:
    """Collect mempool.space-compatible fee and mempool state."""
    mempool_info = _dict(client.get("/api/mempool"))
    recommended = _dict(client.get("/api/v1/fees/recommended"))
    histogram = mempool_info.get("fee_histogram")
    histogram_buckets = len(histogram) if isinstance(histogram, list) else 0
    return {
        "mempool": {
            "tx_count": _num(mempool_info.get("count")),
            "vsize_vb": _num(mempool_info.get("vsize")),
            "total_fee_sat": _num(mempool_info.get("total_fee")),
            "fee_histogram_buckets": histogram_buckets,
        },
        "fees": {
            "fastest_fee_sat_vb": _num(recommended.get("fastestFee")),
            "half_hour_fee_sat_vb": _num(recommended.get("halfHourFee")),
            "hour_fee_sat_vb": _num(recommended.get("hourFee")),
            "economy_fee_sat_vb": _num(recommended.get("economyFee")),
            "minimum_fee_sat_vb": _num(recommended.get("minimumFee")),
        },
    }


def build_bitcoin_regime(
    *,
    node: Mapping[str, Any],
    mempool: Mapping[str, Any],
    fees: Mapping[str, Any],
    source_flags: Mapping[str, Any],
    errors: Iterable[Mapping[str, str]] = (),
    history_records: Iterable[Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    as_of = now or datetime.now(UTC)
    node_out = _node_payload(node)
    mempool_out = _mempool_payload(mempool, fees)
    stress = classify_mempool_stress(mempool_out)
    history = tuple(history_records)
    fastest_fee = _num(mempool_out.get("fastest_fee_sat_vb"))
    mempool_vsize = _num(mempool_out.get("vsize_vb"))
    mempool_count = _num(mempool_out.get("tx_count"))
    source_status = _source_status(node_out, mempool_out, source_flags, tuple(errors))
    research_tags = _research_tags(stress, node_out, source_status)
    features = {
        "stress_score": stress["score"],
        "fee_pressure_score": stress["fee_score"],
        "mempool_pressure_score": stress["mempool_score"],
        "fastest_fee_sat_vb": fastest_fee,
        "mempool_vsize_vb": mempool_vsize,
        "mempool_tx_count": mempool_count,
        "fee_spike_z": _history_z_score(
            fastest_fee, history, ("features", "fastest_fee_sat_vb")
        ),
        "mempool_pressure_z": _history_z_score(
            mempool_vsize, history, ("features", "mempool_vsize_vb")
        ),
    }
    return {
        "schema_version": BITCOIN_REGIME_ID,
        "generated_at": as_of.isoformat(),
        "as_of": as_of.isoformat(),
        "mode": "research_only_bitcoin_network_context",
        "source": {
            "status": source_status,
            "fixture": bool(source_flags.get("fixture")),
            "bitcoin_core_rpc": bool(source_flags.get("bitcoin_core_rpc")),
            "mempool_api": bool(source_flags.get("mempool_api")),
            "errors": list(errors),
        },
        "policy": {
            "status": "research_only",
            "can_trade": False,
            "can_promote": False,
            "live_orders_enabled": False,
            "orders_allowed": False,
            "wallet_rpc_allowed": False,
            "principle": (
                "Bitcoin network context may tag research and replay windows, "
                "but it never creates orders or promotion rights"
            ),
        },
        "can_trade": False,
        "can_promote": False,
        "live_orders_enabled": False,
        "node": node_out,
        "mempool": {
            **mempool_out,
            "stress_state": stress["state"],
            "stress_reason": stress["reason"],
        },
        "features": features,
        "research_tags": research_tags,
        "summary": {
            "stress_state": stress["state"],
            "stress_score": stress["score"],
            "source_status": source_status,
            "primary_reason": stress["reason"],
            "can_trade": False,
            "can_promote": False,
        },
    }


def classify_mempool_stress(mempool: Mapping[str, Any]) -> dict[str, Any]:
    tx_count = _num(mempool.get("tx_count"))
    vsize = _num(mempool.get("vsize_vb"))
    fastest_fee = _num(mempool.get("fastest_fee_sat_vb"))
    half_hour_fee = _num(mempool.get("half_hour_fee_sat_vb"))
    fee = max(fastest_fee, half_hour_fee)

    if tx_count <= 0 and vsize <= 0 and fee <= 0:
        return {
            "state": "missing",
            "score": 0.0,
            "fee_score": 0.0,
            "mempool_score": 0.0,
            "reason": "no usable mempool or fee data",
        }

    fee_score = _bucket(fee, ((5, 0), (15, 1), (35, 2), (75, 3), (150, 4)), 5)
    vsize_score = _bucket(
        vsize,
        (
            (25_000_000, 0),
            (100_000_000, 1),
            (250_000_000, 2),
            (500_000_000, 3),
            (900_000_000, 4),
        ),
        5,
    )
    count_score = _bucket(
        tx_count,
        ((30_000, 0), (100_000, 1), (200_000, 2), (350_000, 3), (550_000, 4)),
        5,
    )
    mempool_score = max(vsize_score, count_score)
    score = round(fee_score * 1.25 + mempool_score, 2)

    if score >= 8:
        state = "panic"
    elif score >= 5:
        state = "stressed"
    elif score >= 2:
        state = "building"
    else:
        state = "calm"
    reason = (
        f"fee_score={fee_score:.1f}, mempool_score={mempool_score:.1f}, "
        f"fastest_fee={fee:.1f} sat/vB, tx_count={tx_count:.0f}, vsize={vsize:.0f} vB"
    )
    return {
        "state": state,
        "score": score,
        "fee_score": float(fee_score),
        "mempool_score": float(mempool_score),
        "reason": reason,
    }


def publish_bitcoin_regime(
    payload: Mapping[str, Any],
    out: Path = DEFAULT_LATEST,
    history: Path | None = DEFAULT_HISTORY,
) -> None:
    _write_json_atomic(payload, out)
    if history is not None:
        history.parent.mkdir(parents=True, exist_ok=True)
        with open(history, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


def load_history(path: Path, *, limit: int = 500) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines()[-limit:]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return tuple(rows)


def _validate_rpc_method(method: str) -> None:
    method_l = method.lower()
    if method_l not in READ_ONLY_RPC_METHODS:
        raise UnsafeBitcoinRpcMethod(f"Bitcoin RPC method is not allowlisted: {method}")
    if any(token in method_l for token in FORBIDDEN_RPC_TOKENS):
        raise UnsafeBitcoinRpcMethod(f"Bitcoin RPC method is unsafe for VNEDGE: {method}")


def _node_payload(node: Mapping[str, Any]) -> dict[str, Any]:
    if not node:
        return {
            "available": False,
            "synced": None,
            "blocks": 0,
            "headers": 0,
            "verification_progress": 0.0,
        }
    blocks = int(_num(node.get("blocks")))
    headers = int(_num(node.get("headers")))
    synced = bool(node.get("synced", False))
    return {
        "available": True,
        "chain": node.get("chain"),
        "synced": synced,
        "blocks": blocks,
        "headers": headers,
        "height_gap": max(0, headers - blocks),
        "verification_progress": _num(node.get("verification_progress")),
        "initial_block_download": bool(node.get("initial_block_download", False)),
        "best_block_hash": node.get("best_block_hash"),
        "pruned": bool(node.get("pruned", False)),
        "tips_count": int(_num(node.get("tips_count"))),
        "stale_tips": int(_num(node.get("stale_tips"))),
    }


def _mempool_payload(mempool: Mapping[str, Any], fees: Mapping[str, Any]) -> dict[str, Any]:
    fastest = _first_num(
        fees.get("fastest_fee_sat_vb"),
        mempool.get("fastest_fee_sat_vb"),
        mempool.get("min_fee_sat_vb"),
    )
    half_hour = _first_num(fees.get("half_hour_fee_sat_vb"), mempool.get("half_hour_fee_sat_vb"))
    return {
        "available": bool(mempool or fees),
        "tx_count": _num(mempool.get("tx_count")),
        "vsize_vb": _num(mempool.get("vsize_vb")),
        "memory_usage_bytes": _num(mempool.get("memory_usage_bytes")),
        "total_fee_sat": _num(mempool.get("total_fee_sat")),
        "fee_histogram_buckets": int(_num(mempool.get("fee_histogram_buckets"))),
        "min_fee_sat_vb": _num(mempool.get("min_fee_sat_vb") or fees.get("minimum_fee_sat_vb")),
        "fastest_fee_sat_vb": fastest,
        "half_hour_fee_sat_vb": half_hour,
        "hour_fee_sat_vb": _num(fees.get("hour_fee_sat_vb")),
        "economy_fee_sat_vb": _num(fees.get("economy_fee_sat_vb")),
    }


def _source_status(
    node: Mapping[str, Any],
    mempool: Mapping[str, Any],
    source_flags: Mapping[str, Any],
    errors: tuple[Mapping[str, str], ...],
) -> str:
    if errors and not bool(mempool.get("available")) and not bool(node.get("available")):
        return "source_error"
    if errors:
        return "partial_error"
    if not any(source_flags.get(k) for k in ("bitcoin_core_rpc", "mempool_api", "fixture")):
        return "missing_config"
    if bool(node.get("available")) and node.get("synced") is False:
        return "node_unsynced"
    if not bool(mempool.get("available")):
        return "mempool_missing"
    return "ok"


def _research_tags(
    stress: Mapping[str, Any],
    node: Mapping[str, Any],
    source_status: str,
) -> list[str]:
    tags = [f"btc_fee_market_{stress['state']}"]
    if stress["state"] in {"stressed", "panic"}:
        tags.append("mempool_pressure_high")
    if source_status != "ok":
        tags.append(f"bitcoin_source_{source_status}")
    if bool(node.get("available")) and node.get("synced") is False:
        tags.append("bitcoin_node_unsynced")
    return tags


def _history_z_score(
    current: float,
    history_records: Iterable[Mapping[str, Any]],
    path: tuple[str, ...],
) -> float:
    values = [_nested_num(row, path) for row in history_records]
    values = [value for value in values if value > 0]
    if len(values) < 5 or current <= 0:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    std = variance ** 0.5
    if std <= 0:
        return 0.0
    return round((current - mean) / std, 3)


def _nested_num(row: Mapping[str, Any], path: tuple[str, ...]) -> float:
    value: Any = row
    for key in path:
        if not isinstance(value, Mapping):
            return 0.0
        value = value.get(key)
    return _num(value)


def _btc_per_kvb_to_sat_vb(value: Any) -> float:
    # Bitcoin Core returns BTC/kvB; sat/vB = BTC/kvB * 100_000_000 / 1000.
    return _num(value) * 100_000.0


def _bucket(value: float, thresholds: tuple[tuple[float, int], ...], overflow: int) -> int:
    for threshold, score in thresholds:
        if value < threshold:
            return score
    return overflow


def _first_num(*values: Any) -> float:
    for value in values:
        num = _num(value)
        if num > 0:
            return num
    return 0.0


def _num(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _httpx() -> Any:
    try:
        import httpx  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise RuntimeError("httpx is required for live Bitcoin regime collection") from exc
    return httpx


def _write_json_atomic(payload: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run VNEDGE Bitcoin regime sensor")
    parser.add_argument("--out", default=str(DEFAULT_LATEST))
    parser.add_argument("--history", default=str(DEFAULT_HISTORY))
    parser.add_argument("--interval-seconds", type=int, default=0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fixture", default="", help="offline fixture JSON for tests/backfills")
    parser.add_argument("--bitcoin-rpc-url", default=os.getenv("BITCOIN_RPC_URL", ""))
    parser.add_argument("--bitcoin-rpc-user", default=os.getenv("BITCOIN_RPC_USER", ""))
    parser.add_argument("--bitcoin-rpc-password", default=os.getenv("BITCOIN_RPC_PASSWORD", ""))
    parser.add_argument("--mempool-api-base", default=os.getenv("MEMPOOL_API_BASE", ""))
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        fixture = _read_fixture(Path(args.fixture)) if args.fixture else None
        bitcoin_core = (
            BitcoinCoreRpcClient(
                args.bitcoin_rpc_url,
                username=args.bitcoin_rpc_user or None,
                password=args.bitcoin_rpc_password or None,
                timeout_seconds=args.timeout_seconds,
            )
            if args.bitcoin_rpc_url else None
        )
        mempool_api = (
            MempoolApiClient(args.mempool_api_base, timeout_seconds=args.timeout_seconds)
            if args.mempool_api_base else None
        )
        history_path = Path(args.history) if args.history else None
        payload = run_bitcoin_regime(
            bitcoin_core=bitcoin_core,
            mempool_api=mempool_api,
            fixture=fixture,
            history_records=load_history(history_path) if history_path else (),
        )
        publish_bitcoin_regime(payload, Path(args.out), history_path)
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(_format_summary(payload))
        if args.once or args.interval_seconds <= 0:
            return 0
        time.sleep(max(args.interval_seconds, 1))


def _read_fixture(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _format_summary(payload: Mapping[str, Any]) -> str:
    summary = _dict(payload.get("summary"))
    source = _dict(payload.get("source"))
    return (
        f"{payload.get('generated_at')} {BITCOIN_REGIME_ID}: "
        f"stress={summary.get('stress_state')} score={summary.get('stress_score')} "
        f"source={source.get('status')} can_trade=false"
    )


if __name__ == "__main__":
    raise SystemExit(main())
