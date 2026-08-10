"""Pine-script research knowledge base for public indicator review.

This module is deliberately read-only and artifact-first. TradingView exposes
script metadata and an open-source filter, but many scripts are invite-only or
protected. VNEDGE therefore stores provenance, source hashes, review decisions,
and backtest evidence; it does not vendor bulk third-party Pine source into the
repo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from html import unescape
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Literal
from urllib.error import URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


PortabilityVerdict = Literal[
    "PORTABLE",
    "PORTABLE_WITH_CHANGES",
    "RESEARCH_ONLY",
    "BLOCKED_NO_SOURCE",
    "BLOCKED_REPAINT_RISK",
]
ScriptKind = Literal["indicator", "strategy", "library", "unknown"]
BacktestStatus = Literal["queued", "running", "passed", "failed", "blocked", "not_applicable"]

DEFAULT_TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h")
DEFAULT_VENUES: tuple[str, ...] = ("binanceusdm", "bybit", "delta_india")
DEFAULT_PINE_SOURCE_DIR = Path("research/pine_scripts/sources")
DEFAULT_PINE_KB_PATH = Path("research/pine_scripts/pine_research_kb.json")
DEFAULT_PINE_EXTRACTION_MANIFEST = Path("research/pine_scripts/extraction_manifest.jsonl")
PINE_SOURCE_SUFFIXES = (".pine", ".pinescript", ".txt")
TRADINGVIEW_BASE_URL = "https://www.tradingview.com"
DEFAULT_TRADINGVIEW_DISCOVERY_URLS: tuple[str, ...] = (
    "https://www.tradingview.com/scripts/",
    "https://www.tradingview.com/scripts/indicators/",
    "https://www.tradingview.com/scripts/strategies/",
    "https://www.tradingview.com/scripts/crypto/",
    "https://www.tradingview.com/scripts/bitcoin/",
    "https://www.tradingview.com/scripts/ethereum/",
    "https://www.tradingview.com/scripts/scalping/",
    "https://www.tradingview.com/scripts/daytrading/",
    "https://www.tradingview.com/scripts/swingtrading/",
    "https://www.tradingview.com/scripts/trendanalysis/",
    "https://www.tradingview.com/scripts/technicalindicators/",
    "https://www.tradingview.com/scripts/volume/",
    "https://www.tradingview.com/scripts/supportandresistance/",
    "https://www.tradingview.com/scripts/smartmoneyconcepts/",
    "https://www.tradingview.com/scripts/liquidity/",
    "https://www.tradingview.com/scripts/orderblocks/",
    "https://www.tradingview.com/scripts/fairvaluegap/",
    "https://www.tradingview.com/scripts/supplyanddemand/",
    "https://www.tradingview.com/scripts/rsi/",
    "https://www.tradingview.com/scripts/macd/",
    "https://www.tradingview.com/scripts/supertrend/",
    "https://www.tradingview.com/scripts/vwap/",
    "https://www.tradingview.com/scripts/bollingerbands/",
)
TRADINGVIEW_SCRIPT_RE = re.compile(
    r"(?:https?://(?:www\.|in\.)?tradingview\.com)?/script/"
    r"[A-Za-z0-9]+-[^\"'<>\s?#]+/?(?:#[^\"'<>\s]+)?"
)
TRADINGVIEW_CATALOG_RE = re.compile(
    r"(?:https?://(?:www\.|in\.)?tradingview\.com)?/scripts/"
    r"[A-Za-z0-9][^\"'<>\s?#]*/?"
)
TITLE_STOPWORDS = frozenset({
    "a",
    "ai",
    "algo",
    "and",
    "auto",
    "bot",
    "crypto",
    "for",
    "full",
    "god",
    "indicator",
    "mode",
    "pro",
    "risk",
    "signals",
    "strategy",
    "the",
    "view",
    "with",
    "zones",
})
MECHANISM_PRIORITY = {
    "orderflow": 24,
    "liquidity": 22,
    "structure": 20,
    "breakout": 16,
    "momentum": 14,
    "trend": 12,
    "volume": 10,
    "volatility": 8,
    "mtf": 4,
    "risk_plan": 4,
}


@dataclass(frozen=True)
class PineBacktestCell:
    timeframe: str
    status: BacktestStatus = "queued"
    venues: tuple[str, ...] = DEFAULT_VENUES
    samples: int = 0
    avg_net_bps: float | None = None
    profit_factor: float | None = None
    win_rate_pct: float | None = None
    blocker: str = "awaiting VNEDGE port and replay"


@dataclass(frozen=True)
class PineReviewRecord:
    script_id: str
    title: str
    url: str
    author: str = ""
    kind: ScriptKind = "unknown"
    source_available: bool = False
    source_license: str = "unknown"
    source_sha256: str | None = None
    source_lines: int = 0
    tags: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    crypto_portability: PortabilityVerdict = "BLOCKED_NO_SOURCE"
    crypto_fit_score: int = 0
    porting_notes: tuple[str, ...] = ()
    ai_uplift_ideas: tuple[str, ...] = ()
    backtests: tuple[PineBacktestCell, ...] = field(default_factory=tuple)
    decision: str = "WAIT_FOR_PUBLIC_SOURCE"
    reviewed_at: str | None = None
    can_trade: bool = False
    can_promote: bool = False
    requires_untouched_judgment: bool = True

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["backtests"] = [asdict(cell) for cell in self.backtests]
        return payload


def empty_pine_research_payload() -> dict:
    source_extraction = _empty_extraction_summary()
    records: list[dict] = []
    backtest_evidence = _empty_backtest_evidence_summary()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "fallback_empty",
        "summary": {
            "total": 0,
            "source_backed": 0,
            "catalog_only": 0,
            "reconciled_catalog_matches": 0,
            "port_queue": 0,
            "source_requests": 0,
            "portable": 0,
            "needs_source": 0,
            "research_only": 0,
            "blocked_repaint": 0,
            "backtests_queued": 0,
        },
        "backtest_evidence": backtest_evidence,
        "source_extraction": source_extraction,
        "coverage_audit": build_pine_coverage_audit(
            records,
            source_extraction=source_extraction,
            backtest_evidence=backtest_evidence,
        ),
        "records": records,
        "priorities": [],
        "policy": _policy(),
        "operator_answer": (
            "Pine research KB unavailable; publish research/pine_scripts/"
            "pine_research_kb.json from the reviewer pipeline."
        ),
        "can_trade": False,
        "can_promote": False,
    }


def default_pine_research_payload() -> dict:
    """Seed the page with a serious workflow before the crawler publishes."""

    records = (
        PineReviewRecord(
            script_id="tradingview_catalog",
            title="TradingView public scripts catalog",
            url="https://www.tradingview.com/scripts/",
            author="TradingView community",
            kind="unknown",
            source_available=False,
            tags=("catalog", "discovery", "requires_open_source_filter"),
            features=("metadata_discovery", "open_source_filter"),
            risks=(
                "catalog pages do not reliably expose Pine source in static HTML",
                "protected/invite-only scripts cannot be copied or ported",
            ),
            crypto_portability="BLOCKED_NO_SOURCE",
            crypto_fit_score=15,
            porting_notes=(
                "Use the open-source filter or user-supplied Pine export before porting.",
                "Store source hash/provenance, not bulk source, in the VNEDGE KB.",
            ),
            ai_uplift_ideas=(
                "Cluster scripts by mechanism: trend, breakout, mean reversion, volume, SMC, exits.",
                "Reject duplicate visual overlays unless they add executable edge after fees.",
            ),
            backtests=_queued_backtests("blocked", "source unavailable from catalog page"),
            decision="DISCOVER_OPEN_SOURCE_ONLY",
            reviewed_at=datetime.now(UTC).isoformat(),
        ),
        PineReviewRecord(
            script_id="luxara_live_plan_qtm_v1",
            title="Luxara Live Plan - QTM Matched",
            url="user_supplied_pine:8985eadc",
            author="user supplied",
            kind="indicator",
            source_available=True,
            tags=("atr_trail", "rsi", "structure_midline", "trade_plan"),
            features=("ATR/QTM trail", "EMA/RSI grade", "TP ladder", "expected edge gate"),
            risks=("raw visual labels were negative after cost", "same-data tuned defaults"),
            crypto_portability="PORTABLE_WITH_CHANGES",
            crypto_fit_score=68,
            porting_notes=(
                "Keep QTM flips causal and replace fixed chart points with ATR/bps exits.",
                "Only high-room, high-volume long defaults survived the first VM replay.",
            ),
            ai_uplift_ideas=(
                "Use as a candidate feature inside the edge model, not as a standalone signal.",
                "Retest exact defaults on an untouched window before any paper trial.",
            ),
            backtests=(
                PineBacktestCell(
                    timeframe="15m",
                    status="failed",
                    samples=248,
                    avg_net_bps=27.30,
                    profit_factor=1.61,
                    win_rate_pct=50.8,
                    blocker="same-data research only; needs untouched judgment",
                ),
            )
            + _queued_backtests("queued", "awaiting untouched-window judgment", skip=("15m",)),
            decision="RESEARCH_CANDIDATE_NO_PROMOTION",
            reviewed_at=datetime.now(UTC).isoformat(),
        ),
        PineReviewRecord(
            script_id="luxara_break_bounce_v27_v1",
            title="Luxara Break & Bounce Teaching View V27",
            url="user_supplied_pine:be3cf729",
            author="user supplied",
            kind="indicator",
            source_available=True,
            tags=("range_box", "breakout", "volume_impulse", "tp_sl_plan"),
            features=("prior-bar setup box", "wick preview telemetry", "confirmed close breakout"),
            risks=("broad breakouts failed after costs", "only sparse short/tight-box pulse found"),
            crypto_portability="RESEARCH_ONLY",
            crypto_fit_score=44,
            porting_notes=(
                "Preview labels stay telemetry-only.",
                "Default scanner is short-only A-grade high-volume tight-box, but not promotable.",
            ),
            ai_uplift_ideas=(
                "Keep as feature-engineering input for downside break classifiers.",
                "Require more history before ETH-only untouched judgment.",
            ),
            backtests=(
                PineBacktestCell(
                    timeframe="15m",
                    status="failed",
                    samples=48,
                    avg_net_bps=13.05,
                    profit_factor=None,
                    win_rate_pct=None,
                    blocker="under-sampled and below 25 bps edge floor",
                ),
                PineBacktestCell(
                    timeframe="5m",
                    status="failed",
                    samples=1,
                    avg_net_bps=-63.84,
                    profit_factor=None,
                    win_rate_pct=0.0,
                    blocker="Delta ETH only; no broad 5m data coverage yet",
                ),
            )
            + _queued_backtests("queued", "needs wider timeframe replay", skip=("5m", "15m")),
            decision="TELEMETRY_ONLY",
            reviewed_at=datetime.now(UTC).isoformat(),
        ),
    )
    return build_pine_research_payload(records, source="default_seed")


def load_pine_research_payload(path: Path | None) -> dict:
    """Load a published KB artifact with a defensive fallback."""

    if path is None or not path.exists():
        return default_pine_research_payload()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_pine_research_payload()
    if not isinstance(raw, dict):
        return empty_pine_research_payload()
    records = raw.get("records")
    if not isinstance(records, list):
        return {**empty_pine_research_payload(), "source": str(path)}
    normalized = []
    for record in records:
        if isinstance(record, dict):
            normalized.append(_normalize_record(record))
    enriched = enrich_pine_research_records(normalized)
    backtest_evidence = (
        raw.get("backtest_evidence")
        if isinstance(raw.get("backtest_evidence"), dict)
        else _empty_backtest_evidence_summary()
    )
    source_extraction = (
        raw.get("source_extraction")
        if isinstance(raw.get("source_extraction"), dict)
        else _empty_extraction_summary()
    )
    coverage_audit = build_pine_coverage_audit(
        enriched,
        source_extraction=source_extraction,
        backtest_evidence=backtest_evidence,
        previous_coverage=(
            raw.get("coverage_audit")
            if isinstance(raw.get("coverage_audit"), dict)
            else None
        ),
    )
    return {
        "generated_at": str(raw.get("generated_at") or datetime.now(UTC).isoformat()),
        "source": str(raw.get("source") or path),
        "summary": summarize_records(enriched),
        "backtest_evidence": backtest_evidence,
        "records": enriched,
        "priorities": _priority_queue(enriched),
        "source_extraction": source_extraction,
        "coverage_audit": coverage_audit,
        "policy": raw.get("policy") if isinstance(raw.get("policy"), dict) else _policy(),
        "operator_answer": str(
            raw.get("operator_answer")
            or "Pine research KB loaded; reviews are research-only until backtested."
        ),
        "can_trade": False,
        "can_promote": False,
    }


def build_pine_research_payload(
    records: Iterable[PineReviewRecord],
    *,
    source: str,
) -> dict:
    rows = enrich_pine_research_records(record.to_dict() for record in records)
    source_extraction = _empty_extraction_summary()
    backtest_evidence = _empty_backtest_evidence_summary()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source,
        "summary": summarize_records(rows),
        "backtest_evidence": backtest_evidence,
        "records": rows,
        "priorities": _priority_queue(rows),
        "source_extraction": source_extraction,
        "coverage_audit": build_pine_coverage_audit(
            rows,
            source_extraction=source_extraction,
            backtest_evidence=backtest_evidence,
        ),
        "policy": _policy(),
        "operator_answer": (
            "Pine reviews are a research funnel. A script can become a VNEDGE "
            "candidate only after source/provenance, causal port, multi-TF replay, "
            "cost-aware route proof, and untouched-window judgment."
        ),
        "can_trade": False,
        "can_promote": False,
    }


def publish_pine_research_kb(
    *,
    source_dir: Path | str | None = DEFAULT_PINE_SOURCE_DIR,
    source_files: Iterable[Path | str] = (),
    catalog_urls: Iterable[str] = (),
    catalog_discovery_urls: Iterable[str] = (),
    catalog_html_files: Iterable[Path | str] = (),
    extraction_manifest_files: Iterable[Path | str] = (),
    output_path: Path | str = DEFAULT_PINE_KB_PATH,
    include_defaults: bool = True,
    max_catalog_records: int = 250,
    catalog_discovery_depth: int = 0,
    max_catalog_pages: int = 40,
    discovered_total: int | None = None,
    source_label: str = "pine_research_publisher",
) -> dict:
    """Review public/user-supplied Pine files and write the dashboard KB.

    This intentionally works from local files only. TradingView protected or
    invite-only source must not be scraped into VNEDGE; those records stay
    metadata-only until the user supplies lawful source.
    """

    records: list[dict] = []
    extraction_entries = load_pine_extraction_manifest(extraction_manifest_files)
    extraction_by_path = _extraction_entries_by_output(extraction_entries)
    if include_defaults:
        records.extend(default_pine_research_payload()["records"])
    for path in discover_pine_source_files(source_dir, source_files):
        source = path.read_text(encoding="utf-8")
        record = review_pine_source(
            script_id=_script_id_from_path(path),
            title=_title_from_source(source, path.stem),
            url=f"user_supplied_pine:{path.name}",
            source=source,
            author=_author_from_source(source),
            source_license=_license_from_source(source),
        )
        record_dict = record.to_dict()
        _apply_extraction_provenance(
            record_dict,
            _extraction_entry_for_source_path(path, extraction_by_path),
        )
        records.append(record_dict)
    for url in discover_tradingview_catalog_urls(
        catalog_urls=catalog_urls,
        catalog_discovery_urls=catalog_discovery_urls,
        catalog_html_files=catalog_html_files,
        max_records=max_catalog_records,
        discovery_depth=catalog_discovery_depth,
        max_pages=max_catalog_pages,
    ):
        records.append(review_tradingview_catalog_script(url).to_dict())
    payload = _build_payload_from_dicts(
        _dedupe_record_dicts(records),
        source=source_label,
        source_extraction=summarize_extraction_manifest(extraction_entries),
        discovered_total=discovered_total,
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def discover_pine_source_files(
    source_dir: Path | str | None = DEFAULT_PINE_SOURCE_DIR,
    source_files: Iterable[Path | str] = (),
) -> tuple[Path, ...]:
    """Return local Pine source files in stable order."""

    discovered: list[Path] = []
    for item in source_files:
        path = Path(item)
        if path.exists() and path.is_file() and _is_pine_source_path(path):
            discovered.append(path)
    if source_dir is not None:
        root = Path(source_dir)
        if root.exists():
            discovered.extend(
                path for path in root.rglob("*")
                if path.is_file() and _is_pine_source_path(path)
            )
    seen: set[Path] = set()
    out: list[Path] = []
    for path in sorted(discovered, key=lambda p: str(p)):
        resolved = path.resolve()
        if resolved in seen:
            continue
        out.append(path)
        seen.add(resolved)
    return tuple(out)


def load_pine_extraction_manifest(
    manifest_files: Iterable[Path | str] = (),
) -> tuple[dict, ...]:
    """Load JSONL source-extraction attempts from browser/operator runs.

    The manifest is a provenance trail, not a source transport. Source files
    remain gitignored artifacts; committed KB rows keep only URL/status/hash
    evidence so a later operator can audit what was actually opened.
    """

    entries: list[dict] = []
    for item in manifest_files:
        path = Path(item)
        if not path.exists() or not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                normalized = _normalize_extraction_entry(parsed)
                if normalized:
                    entries.append(normalized)
    return tuple(entries)


def summarize_extraction_manifest(entries: Iterable[dict]) -> dict:
    rows = tuple(entries)
    raw_statuses = Counter(str(row.get("status") or "unknown") for row in rows)
    current_rows = _latest_extraction_entries(rows)
    statuses = Counter(str(row.get("status") or "unknown") for row in current_rows)
    extracted = [row for row in rows if row.get("status") == "extracted"]
    current_extracted = [
        row for row in current_rows if row.get("status") == "extracted"
    ]
    source_tab_failures = sum(
        count
        for status, count in statuses.items()
        if status.startswith("failed_no_source")
        or status.startswith("blocked_missing_source")
        or status.startswith("blocked_no_source")
    )
    blocked = sum(
        count
        for status, count in statuses.items()
        if status.startswith("blocked")
    ) + sum(
        count
        for status, count in statuses.items()
        if status.startswith("failed_no_source")
    )
    latest_at = max(
        (str(row.get("started_at") or "") for row in rows if row.get("started_at")),
        default="",
    )
    latest_success_at = max(
        (str(row.get("started_at") or "") for row in extracted if row.get("started_at")),
        default="",
    )
    return {
        "attempted": len(rows),
        "current_attempts": len(current_rows),
        "extracted": len(current_extracted),
        "blocked": blocked,
        "errors": statuses["error"],
        "retryable_errors": statuses["error"],
        "source_tab_failures": source_tab_failures,
        "status_counts": dict(sorted(statuses.items())),
        "raw_status_counts": dict(sorted(raw_statuses.items())),
        "raw_extracted": len(extracted),
        "latest_attempt_at": latest_at,
        "latest_success_at": latest_success_at,
    }


def _latest_extraction_entries(entries: Iterable[dict]) -> tuple[dict, ...]:
    """Return the latest manifest row per TradingView URL or output artifact."""

    by_key: dict[str, dict] = {}
    unkeyed: list[dict] = []
    for row in entries:
        key = str(row.get("url") or "").strip()
        if not key:
            key = str(row.get("output") or row.get("output_name") or "").strip()
        if not key:
            unkeyed.append(row)
            continue
        existing = by_key.get(key)
        if existing is None or str(row.get("started_at") or "") >= str(
            existing.get("started_at") or ""
        ):
            by_key[key] = row
    return tuple([*by_key.values(), *unkeyed])


def discover_tradingview_catalog_urls(
    *,
    catalog_urls: Iterable[str] = (),
    catalog_discovery_urls: Iterable[str] = (),
    catalog_html_files: Iterable[Path | str] = (),
    max_records: int = 250,
    discovery_depth: int = 0,
    max_pages: int = 40,
) -> tuple[str, ...]:
    """Discover TradingView script URLs from catalog/profile/tag pages.

    TradingView's public catalog exposes script cards in HTML for some pages
    and via client-side rendering for others. This importer intentionally
    collects URL metadata only; Pine source must arrive via open/user-supplied
    local files before any strategy port or replay can happen.
    """

    urls: list[str] = []
    pending_pages: deque[tuple[str, int]] = deque()
    for url in catalog_urls:
        cleaned = _clean_url(url)
        if not cleaned:
            continue
        if _is_tradingview_script_url(cleaned):
            urls.append(cleaned)
            continue
        page = _normalize_tradingview_catalog_url(cleaned)
        if page:
            pending_pages.append((page, 0))
    for url in catalog_discovery_urls:
        page = _normalize_tradingview_catalog_url(url)
        if page:
            pending_pages.append((page, 0))
    seen_pages: set[str] = set()
    max_depth = max(0, discovery_depth)
    page_budget = max(0, max_pages)
    while pending_pages and len(seen_pages) < page_budget and len(urls) < max_records:
        page_url, depth = pending_pages.popleft()
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)
        html = _fetch_catalog_html(page_url)
        urls.extend(extract_tradingview_script_urls(html, base_url=page_url))
        if depth >= max_depth:
            continue
        for next_page in extract_tradingview_catalog_page_urls(html, base_url=page_url):
            if next_page in seen_pages:
                continue
            pending_pages.append((next_page, depth + 1))
    for item in catalog_html_files:
        path = Path(item)
        if not path.exists() or not path.is_file():
            continue
        urls.extend(
            extract_tradingview_script_urls(
                path.read_text(encoding="utf-8"),
                base_url=TRADINGVIEW_BASE_URL,
            )
        )
    return _dedupe_urls(urls, limit=max_records)


def extract_tradingview_script_urls(
    html: str,
    *,
    base_url: str = TRADINGVIEW_BASE_URL,
) -> tuple[str, ...]:
    """Extract normalized TradingView script URLs from a public HTML payload."""

    found = []
    for match in TRADINGVIEW_SCRIPT_RE.finditer(html):
        found.append(_normalize_tradingview_url(match.group(0), base_url=base_url))
    return _dedupe_urls(found)


def extract_tradingview_catalog_page_urls(
    html: str,
    *,
    base_url: str = TRADINGVIEW_BASE_URL,
) -> tuple[str, ...]:
    """Extract normalized TradingView catalog/tag URLs from HTML."""

    found = []
    for match in TRADINGVIEW_CATALOG_RE.finditer(html):
        normalized = _normalize_tradingview_catalog_url(match.group(0), base_url=base_url)
        if normalized:
            found.append(normalized)
    return _dedupe_plain_urls(found)


def review_tradingview_catalog_script(url: str) -> PineReviewRecord:
    """Create a metadata-only research row for a TradingView script URL."""

    normalized = _normalize_tradingview_url(url)
    slug = _slug_from_script_url(normalized)
    title = _title_from_slug(slug)
    lower = title.lower()
    features = _detect_features(lower)
    risks = {
        "catalog_metadata_only",
        "source_not_collected",
    }
    if not features:
        features = _detect_features(slug.lower().replace("-", " "))
    fit = max(5, min(55, _crypto_fit_score(features, risks, lower) - 18))
    return PineReviewRecord(
        script_id=_script_id_from_url(normalized),
        title=title,
        url=normalized,
        author="TradingView community",
        kind="unknown",
        source_available=False,
        source_license="unknown",
        source_lines=0,
        tags=tuple(sorted({"catalog", *features})),
        features=tuple(sorted(features)),
        risks=tuple(sorted(risks)),
        crypto_portability="BLOCKED_NO_SOURCE",
        crypto_fit_score=fit,
        porting_notes=(
            "Catalog discovery only: fetch or supply lawful open Pine source before porting.",
            "Do not treat screenshots, labels, likes, or comments as executable edge.",
        ),
        ai_uplift_ideas=(
            "Prioritize source requests for scripts with crypto-fit mechanics matching VNEDGE gaps.",
            "Cluster catalog backlog by mechanism, then port one representative per cluster.",
        ),
        backtests=_queued_backtests("blocked", "catalog URL discovered; Pine source not available"),
        decision="WAIT_FOR_OPEN_SOURCE_OR_USER_EXPORT",
        reviewed_at=datetime.now(UTC).isoformat(),
    )


def _build_payload_from_dicts(
    records: Iterable[dict],
    *,
    source: str,
    source_extraction: dict | None = None,
    discovered_total: int | None = None,
) -> dict:
    rows = enrich_pine_research_records(records)
    extraction = source_extraction or _empty_extraction_summary()
    backtest_evidence = _empty_backtest_evidence_summary()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source,
        "summary": summarize_records(rows),
        "backtest_evidence": backtest_evidence,
        "records": rows,
        "priorities": _priority_queue(rows),
        "source_extraction": extraction,
        "coverage_audit": build_pine_coverage_audit(
            rows,
            source_extraction=extraction,
            backtest_evidence=backtest_evidence,
            discovered_total=discovered_total,
        ),
        "policy": _policy(),
        "operator_answer": (
            "Pine KB published from local public/user-supplied source files "
            "and accessible TradingView catalog metadata. "
            "Every record remains research-only until VNEDGE port/replay and "
            "untouched-window judgment clear."
        ),
        "can_trade": False,
        "can_promote": False,
    }


def enrich_pine_research_records(records: Iterable[dict]) -> list[dict]:
    """Normalize, reconcile catalog/source matches, and add triage metadata."""

    rows = [_normalize_record(record) for record in records]
    reconciled = _reconcile_catalog_source_matches(rows)
    enriched = [_with_priority_fields(record) for record in reconciled]
    return sorted(
        enriched,
        key=lambda row: (
            -int(row.get("priority_score") or 0),
            str(row.get("script_id") or ""),
        ),
    )


def _reconcile_catalog_source_matches(records: Iterable[dict]) -> list[dict]:
    source_rows = [
        dict(row)
        for row in records
        if row.get("source_available")
    ]
    catalog_rows = [
        dict(row)
        for row in records
        if not row.get("source_available") and "/script/" in str(row.get("url") or "")
    ]
    other_rows = [
        dict(row)
        for row in records
        if not row.get("source_available") and "/script/" not in str(row.get("url") or "")
    ]
    matched_catalog_ids: set[str] = set()
    for catalog in catalog_rows:
        best_idx = -1
        best_score = 0.0
        for idx, source in enumerate(source_rows):
            score = _title_similarity(
                str(source.get("title") or source.get("script_id") or ""),
                str(catalog.get("title") or catalog.get("script_id") or ""),
            )
            if score > best_score:
                best_idx = idx
                best_score = score
        if best_idx < 0 or best_score < 0.82:
            continue
        source = dict(source_rows[best_idx])
        catalog_url = str(catalog.get("url") or "")
        catalog_id = str(catalog.get("script_id") or "")
        catalog_urls = _append_unique(source.get("catalog_urls"), catalog_url)
        catalog_ids = _append_unique(source.get("catalog_script_ids"), catalog_id)
        source["catalog_urls"] = catalog_urls
        source["catalog_script_ids"] = catalog_ids
        source["catalog_match_score"] = round(best_score, 3)
        source["discovery_status"] = "SOURCE_BACKED_CATALOG_MATCH"
        source["tags"] = tuple(sorted({*source.get("tags", ()), "catalog_match"}))
        source_rows[best_idx] = source
        matched_catalog_ids.add(catalog_id)

    unmatched_catalog = [
        row
        for row in catalog_rows
        if str(row.get("script_id") or "") not in matched_catalog_ids
    ]
    return [*source_rows, *unmatched_catalog, *other_rows]


def _with_priority_fields(record: dict) -> dict:
    row = dict(record)
    mechanism = _mechanism_cluster(row)
    next_action = _next_action(row)
    row["mechanism"] = mechanism
    row["priority_score"] = _priority_score(row, mechanism)
    row["next_action"] = next_action
    row["priority_reason"] = _priority_reason(row, mechanism, next_action)
    row.setdefault("discovery_status", "SOURCE_BACKED" if row.get("source_available") else "CATALOG_ONLY")
    row["source_status"] = _source_status(row)
    row["source_explanation"] = _source_explanation(row)
    row["source_next_step"] = _source_next_step(row)
    return row


def _empty_extraction_summary() -> dict:
    return {
        "attempted": 0,
        "current_attempts": 0,
        "extracted": 0,
        "blocked": 0,
        "errors": 0,
        "status_counts": {},
        "raw_status_counts": {},
        "raw_extracted": 0,
        "latest_attempt_at": "",
        "latest_success_at": "",
    }


def _empty_backtest_evidence_summary() -> dict:
    return {
        "evidence_id": "none",
        "completed_cells": 0,
        "positive_completed_cells": 0,
        "status_counts": {},
        "best_positive_avg_net_bps": None,
        "best_positive_profit_factor": None,
        "best_positive_cell": None,
        "best_completed_avg_net_bps": None,
        "best_completed_profit_factor": None,
        "best_completed_cell": None,
        "headline_verdict": "NO_BACKTEST_EVIDENCE",
        "ports_with_evidence": 0,
        "can_trade": False,
        "can_promote": False,
    }


def _normalize_extraction_entry(entry: dict) -> dict:
    status = str(entry.get("status") or "").strip().lower()
    if not status:
        return {}
    output = str(entry.get("output") or "").strip()
    url = _normalize_tradingview_url(str(entry.get("url") or ""))
    normalized = {
        "started_at": str(entry.get("started_at") or ""),
        "status": status,
        "title": str(entry.get("title") or ""),
        "url": url or str(entry.get("url") or ""),
        "output": output,
        "output_name": Path(output).name if output else "",
        "source_lines": _bounded_int(entry.get("source_lines"), 0, 1_000_000),
        "source_sha256": str(entry.get("source_sha256") or ""),
        "priority_score": _bounded_int(entry.get("priority_score"), 0, 100),
        "mechanism": str(entry.get("mechanism") or ""),
        "error": str(entry.get("error") or ""),
    }
    if (
        not normalized["url"]
        and not normalized["output"]
        and not normalized["error"]
    ):
        return {}
    return normalized


def _extraction_entries_by_output(entries: Iterable[dict]) -> dict[str, dict]:
    by_key: dict[str, dict] = {}
    for entry in entries:
        if entry.get("status") != "extracted":
            continue
        for key in _extraction_output_keys(entry):
            existing = by_key.get(key)
            if existing is None or str(entry.get("started_at") or "") >= str(
                existing.get("started_at") or ""
            ):
                by_key[key] = entry
    return by_key


def _extraction_output_keys(entry: dict) -> tuple[str, ...]:
    keys = []
    output = str(entry.get("output") or "").strip()
    name = str(entry.get("output_name") or "").strip()
    if output:
        keys.append(output)
        try:
            keys.append(str(Path(output).resolve()))
        except OSError:
            pass
        keys.append(Path(output).name)
    if name:
        keys.append(name)
    return tuple(dict.fromkeys(key for key in keys if key))


def _extraction_entry_for_source_path(path: Path, by_output: dict[str, dict]) -> dict | None:
    keys = (str(path), str(path.resolve()), path.name)
    for key in keys:
        entry = by_output.get(key)
        if entry:
            return entry
    return None


def _apply_extraction_provenance(record: dict, entry: dict | None) -> None:
    if not entry:
        return
    url = _normalize_tradingview_url(str(entry.get("url") or ""))
    if url:
        record["url"] = url
        record["catalog_urls"] = _append_unique(record.get("catalog_urls"), url)
        record["discovery_status"] = "SOURCE_BACKED_CATALOG_MATCH"
        record["source_origin"] = "tradingview_open_source_browser"
    record["source_extraction"] = {
        "status": str(entry.get("status") or ""),
        "extracted_at": str(entry.get("started_at") or ""),
        "source_lines": int(entry.get("source_lines") or 0),
        "source_sha256": str(entry.get("source_sha256") or ""),
    }


def _source_status(record: dict) -> str:
    if record.get("source_available"):
        if record.get("catalog_urls"):
            return "SOURCE_BACKED_CATALOG_MATCH"
        url = str(record.get("url") or "")
        if url.startswith("user_supplied_pine:") or url == "user_supplied":
            return "USER_SUPPLIED_SOURCE"
        return "SOURCE_BACKED"
    if "/script/" in str(record.get("url") or ""):
        return "CATALOG_METADATA_ONLY"
    return "SOURCE_MISSING"


def _source_explanation(record: dict) -> str:
    if record.get("source_available"):
        lines = int(record.get("source_lines") or 0)
        digest = str(record.get("source_sha256") or "")[:12]
        suffix = f" Source hash starts {digest}." if digest else ""
        return (
            f"Pine source is present in VNEDGE ({lines} lines), so it can be "
            f"audited, ported causally, and replayed.{suffix}"
        )
    if "/script/" in str(record.get("url") or ""):
        return (
            "This row came from TradingView catalog metadata. The listing gives "
            "title/URL/tags, but not executable Pine source; protected, invite-only, "
            "or closed scripts cannot be copied or honestly backtested by VNEDGE."
        )
    return (
        "No Pine source artifact is attached to this record yet, so VNEDGE can "
        "only keep it as a research idea."
    )


def _source_next_step(record: dict) -> str:
    if record.get("source_available"):
        return "Run causality review, port only causal features, then replay on untouched data."
    if "/script/" in str(record.get("url") or ""):
        return (
            "Open the script page, confirm the author exposes source, then export/paste "
            "the Pine into research/pine_scripts/sources for review."
        )
    return "Attach a .pine/.pinescript/.txt source file before porting or backtesting."


def _priority_queue(records: Iterable[dict], *, limit: int = 25) -> list[dict]:
    queued = sorted(
        (_normalize_record(record) for record in records),
        key=lambda row: (
            -int(row.get("priority_score") or 0),
            str(row.get("script_id") or ""),
        ),
    )
    return [
        {
            "script_id": str(row.get("script_id") or ""),
            "title": str(row.get("title") or ""),
            "url": str(row.get("url") or ""),
            "crypto_portability": str(row.get("crypto_portability") or ""),
            "source_available": bool(row.get("source_available")),
            "mechanism": str(row.get("mechanism") or "unknown"),
            "priority_score": int(row.get("priority_score") or 0),
            "next_action": str(row.get("next_action") or "WAIT"),
            "priority_reason": str(row.get("priority_reason") or ""),
            "source_status": str(row.get("source_status") or ""),
            "source_next_step": str(row.get("source_next_step") or ""),
        }
        for row in queued[:limit]
    ]


def _fetch_catalog_html(url: str, *, timeout_seconds: float = 20.0) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 VNEDGE-PineResearch/1.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return response.read().decode("utf-8", errors="replace")
    except (OSError, URLError, TimeoutError, ValueError):
        return ""


def _dedupe_record_dicts(records: Iterable[dict]) -> tuple[dict, ...]:
    by_id: dict[str, dict] = {}
    for record in records:
        script_id = str(record.get("script_id") or "").strip()
        if not script_id:
            continue
        existing = by_id.get(script_id)
        if existing is None or _record_priority(record) >= _record_priority(existing):
            by_id[script_id] = dict(record)
    return tuple(by_id[key] for key in sorted(by_id))


def _record_priority(record: dict) -> tuple[int, int]:
    return (
        1 if record.get("source_available") else 0,
        int(record.get("source_lines") or 0),
    )


def summarize_records(records: Iterable[dict]) -> dict:
    rows = tuple(records)
    verdicts = Counter(str(row.get("crypto_portability") or "") for row in rows)
    source_backed = sum(1 for row in rows if row.get("source_available"))
    catalog_only = sum(
        1
        for row in rows
        if not row.get("source_available") and "/script/" in str(row.get("url") or "")
    )
    reconciled_catalog_matches = sum(1 for row in rows if row.get("catalog_urls"))
    port_queue = sum(
        1
        for row in rows
        if row.get("source_available")
        and row.get("crypto_portability") in {"PORTABLE", "PORTABLE_WITH_CHANGES"}
    )
    source_requests = sum(
        1
        for row in rows
        if row.get("next_action") == "REQUEST_OPEN_SOURCE_EXPORT"
    )
    backtests_queued = 0
    for row in rows:
        for cell in row.get("backtests") or []:
            if isinstance(cell, dict) and cell.get("status") == "queued":
                backtests_queued += 1
    return {
        "total": len(rows),
        "source_backed": source_backed,
        "catalog_only": catalog_only,
        "reconciled_catalog_matches": reconciled_catalog_matches,
        "port_queue": port_queue,
        "source_requests": source_requests,
        "portable": verdicts["PORTABLE"] + verdicts["PORTABLE_WITH_CHANGES"],
        "needs_source": verdicts["BLOCKED_NO_SOURCE"],
        "research_only": verdicts["RESEARCH_ONLY"],
        "blocked_repaint": verdicts["BLOCKED_REPAINT_RISK"],
        "backtests_queued": backtests_queued,
    }


def build_pine_coverage_audit(
    records: Iterable[dict],
    *,
    source_extraction: dict,
    backtest_evidence: dict | None = None,
    discovered_total: int | None = None,
    previous_coverage: dict | None = None,
) -> dict:
    """Explain exactly what happened to the broad Pine discovery universe.

    The research KB can be source-backed-only after publication, which makes the
    original TradingView discovery count look like it vanished.  This audit keeps
    the non-executable backlog visible without treating catalog metadata as
    source or proof.
    """

    rows = tuple(records)
    summary = summarize_records(rows)
    evidence = backtest_evidence or {}
    status_counts = (
        source_extraction.get("status_counts")
        if isinstance(source_extraction.get("status_counts"), dict)
        else {}
    )
    previous_discovered = _bounded_int(
        (previous_coverage or {}).get("discovered_records"),
        0,
        10_000_000,
    )
    explicit_discovered = _bounded_int(discovered_total, 0, 10_000_000)
    discovered_records = max(
        explicit_discovered,
        previous_discovered,
        summary["total"],
    )
    visible_records = summary["total"]
    source_backed = summary["source_backed"]
    catalog_only_loaded = summary["catalog_only"]
    browser_attempted = _bounded_int(source_extraction.get("attempted"), 0, 10_000_000)
    browser_extracted = _bounded_int(source_extraction.get("extracted"), 0, 10_000_000)
    retryable_errors = _bounded_int(
        source_extraction.get("retryable_errors", source_extraction.get("errors")),
        0,
        10_000_000,
    )
    source_tab_failures = _bounded_int(
        source_extraction.get("source_tab_failures"),
        0,
        10_000_000,
    )
    if not source_tab_failures:
        source_tab_failures = sum(
            _bounded_int(count, 0, 10_000_000)
            for status, count in status_counts.items()
            if str(status).startswith("failed_no_source")
            or str(status).startswith("blocked_missing_source")
            or str(status).startswith("blocked_no_source")
        )
    deduped_source_exports = max(0, browser_extracted - source_backed)
    unloaded_catalog_backlog = max(0, discovered_records - visible_records)
    unresolved_source_backlog = (
        catalog_only_loaded
        + unloaded_catalog_backlog
        + retryable_errors
        + source_tab_failures
    )
    completed_cells = _bounded_int(evidence.get("completed_cells"), 0, 10_000_000)
    positive_completed = _bounded_int(
        evidence.get("positive_completed_cells"),
        0,
        10_000_000,
    )
    failed_completed = 0
    evidence_counts = evidence.get("status_counts")
    if isinstance(evidence_counts, dict):
        failed_completed = _bounded_int(evidence_counts.get("failed"), 0, 10_000_000)

    gaps = []
    if unloaded_catalog_backlog:
        gaps.append({
            "bucket": "CATALOG_BACKLOG_NOT_LOADED",
            "count": unloaded_catalog_backlog,
            "severity": "WARN",
            "action": "REPUBLISH_WITH_CATALOG_DISCOVERY_OR_ARCHIVE_AS_PROTECTED",
            "explanation": (
                "Discovered script metadata exists outside the active KB rows. "
                "Load catalog rows or explicitly archive them as no-source/protected."
            ),
        })
    if retryable_errors:
        gaps.append({
            "bucket": "RETRYABLE_BROWSER_ERRORS",
            "count": retryable_errors,
            "severity": "WARN",
            "action": "RETRY_EXTRACTION_IN_SMALLER_BROWSER_CHUNKS",
            "explanation": "Browser/session errors are intake failures, not strategy verdicts.",
        })
    if source_tab_failures:
        gaps.append({
            "bucket": "SOURCE_TAB_NOT_CAPTURED",
            "count": source_tab_failures,
            "severity": "WARN",
            "action": "REOPEN_PAGE_AND_VERIFY_OPEN_SOURCE_TAB",
            "explanation": "The page was opened, but executable Pine was not captured.",
        })
    if summary["blocked_repaint"]:
        gaps.append({
            "bucket": "CAUSALITY_QUARANTINE",
            "count": summary["blocked_repaint"],
            "severity": "INFO",
            "action": "REWRITE_CLOSED_BAR_HTF_AND_REMOVE_DISPLAY_STATE",
            "explanation": "These source-backed rows cannot be replayed until repaint risk is removed.",
        })
    if summary["port_queue"]:
        gaps.append({
            "bucket": "SOURCE_BACKED_PORT_QUEUE",
            "count": summary["port_queue"],
            "severity": "INFO",
            "action": "PORT_TOP_PRIMITIVES_THEN_REPLAY_WITH_FEES",
            "explanation": "These are the fastest actionable scripts for the alpha factory.",
        })
    if completed_cells and not positive_completed:
        gaps.append({
            "bucket": "COMPLETED_BUT_NEGATIVE_AFTER_COST",
            "count": completed_cells,
            "severity": "WARN",
            "action": "MINE_FAILURES_FOR_EXIT_AND_ROUTING_UPLIFT",
            "explanation": "Completed cells are useful rejection data, but none can promote.",
        })

    replay_status_counts = (
        evidence_counts
        if isinstance(evidence_counts, dict)
        else _backtest_status_counts(rows)
    )
    return {
        "coverage_id": "pine_coverage_auditor_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "discovered_records": discovered_records,
        "visible_records": visible_records,
        "source_backed_records": source_backed,
        "catalog_only_loaded": catalog_only_loaded,
        "unloaded_catalog_backlog": unloaded_catalog_backlog,
        "browser_attempted": browser_attempted,
        "browser_extracted": browser_extracted,
        "retryable_browser_errors": retryable_errors,
        "source_tab_failures": source_tab_failures,
        "deduped_source_exports": deduped_source_exports,
        "unresolved_source_backlog": unresolved_source_backlog,
        "port_ready_records": summary["port_queue"],
        "causality_quarantine": summary["blocked_repaint"],
        "feature_only_records": summary["research_only"],
        "queued_backtest_cells": summary["backtests_queued"],
        "completed_backtest_cells": completed_cells,
        "failed_completed_cells": failed_completed,
        "positive_completed_cells": positive_completed,
        "replay_status_counts": dict(sorted(replay_status_counts.items())),
        "source_status_counts": _source_status_counts(rows),
        "extraction_status_counts": dict(sorted(status_counts.items())),
        "gaps": gaps,
        "operator_answer": _coverage_operator_answer(
            discovered_records=discovered_records,
            visible_records=visible_records,
            source_backed=source_backed,
            unresolved_source_backlog=unresolved_source_backlog,
            port_queue=summary["port_queue"],
            positive_completed=positive_completed,
        ),
        "can_trade": False,
        "can_promote": False,
    }


def _backtest_status_counts(records: Iterable[dict]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in records:
        for cell in row.get("backtests") or []:
            if isinstance(cell, dict):
                counts[str(cell.get("status") or "unknown")] += 1
    return dict(counts)


def _source_status_counts(records: Iterable[dict]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in records:
        counts[_source_status(row)] += 1
    return dict(sorted(counts.items()))


def _coverage_operator_answer(
    *,
    discovered_records: int,
    visible_records: int,
    source_backed: int,
    unresolved_source_backlog: int,
    port_queue: int,
    positive_completed: int,
) -> str:
    if not visible_records:
        return "No Pine KB rows are loaded; publish the source/catalog artifact first."
    if unresolved_source_backlog:
        return (
            f"{visible_records}/{discovered_records} discovered Pine rows are visible, "
            f"with {source_backed} source-backed. {unresolved_source_backlog} rows still "
            "need source, extraction retry, or explicit protected/no-source archiving."
        )
    if port_queue and not positive_completed:
        return (
            f"All visible discovery rows are accounted for. {port_queue} source-backed "
            "rows are ready for causal port/replay, but none has promotable positive "
            "evidence yet."
        )
    return (
        "Pine coverage is accounted for. Continue through causal port, fee-aware "
        "replay, and untouched-window judgment before any promotion."
    )


def review_pine_source(
    *,
    script_id: str,
    title: str,
    url: str,
    source: str,
    author: str = "",
    source_license: str = "unknown",
) -> PineReviewRecord:
    """Heuristic first-pass review for a user-supplied/open-source Pine file.

    This is not the AI council and not a backtest. It creates a structured
    review seed so the real port/replay work can be queued consistently.
    """

    lower = source.lower()
    kind: ScriptKind
    if "strategy(" in lower:
        kind = "strategy"
    elif "indicator(" in lower:
        kind = "indicator"
    elif "library(" in lower:
        kind = "library"
    else:
        kind = "unknown"
    features = _detect_features(lower)
    risks = _detect_risks(lower)
    fit = _crypto_fit_score(features, risks, source)
    verdict: PortabilityVerdict
    if "lookahead_on" in lower or ("request.security" in lower and "barstate.isconfirmed" not in lower):
        verdict = "BLOCKED_REPAINT_RISK"
    elif not source.strip():
        verdict = "BLOCKED_NO_SOURCE"
    elif fit >= 70:
        verdict = "PORTABLE"
    elif fit >= 45:
        verdict = "PORTABLE_WITH_CHANGES"
    else:
        verdict = "RESEARCH_ONLY"
    return PineReviewRecord(
        script_id=script_id,
        title=title,
        url=url,
        author=author,
        kind=kind,
        source_available=bool(source.strip()),
        source_license=source_license,
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        source_lines=len(source.splitlines()),
        tags=tuple(sorted(features)),
        features=tuple(sorted(features)),
        risks=tuple(sorted(risks)),
        crypto_portability=verdict,
        crypto_fit_score=fit,
        porting_notes=_porting_notes(features, risks),
        ai_uplift_ideas=_uplift_ideas(features, risks),
        backtests=_queued_backtests(
            "blocked" if verdict.startswith("BLOCKED") else "queued",
            "blocked by source/repaint risk" if verdict.startswith("BLOCKED") else "awaiting causal VNEDGE port",
        ),
        decision="REVIEW_SEED_READY",
        reviewed_at=datetime.now(UTC).isoformat(),
    )


def _queued_backtests(
    status: BacktestStatus,
    blocker: str,
    *,
    skip: tuple[str, ...] = (),
) -> tuple[PineBacktestCell, ...]:
    return tuple(
        PineBacktestCell(timeframe=tf, status=status, blocker=blocker)
        for tf in DEFAULT_TIMEFRAMES
        if tf not in skip
    )


def _normalize_record(record: dict) -> dict:
    out = dict(record)
    out.setdefault("can_trade", False)
    out.setdefault("can_promote", False)
    out.setdefault("requires_untouched_judgment", True)
    out.setdefault("backtests", [])
    out["crypto_fit_score"] = _bounded_int(out.get("crypto_fit_score"), 0, 100)
    out["priority_score"] = _bounded_int(out.get("priority_score"), 0, 100)
    return out


def _append_unique(existing: object, value: str) -> tuple[str, ...]:
    values: list[str] = []
    if isinstance(existing, (list, tuple)):
        values.extend(str(item) for item in existing if str(item or "").strip())
    if value.strip():
        values.append(value.strip())
    seen: set[str] = set()
    out: list[str] = []
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return tuple(out)


def _title_similarity(left: str, right: str) -> float:
    left_norm = _title_fingerprint(left)
    right_norm = _title_fingerprint(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    if min(len(left_tokens), len(right_tokens)) < 3:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    if overlap < 3:
        return 0.0
    containment = overlap / min(len(left_tokens), len(right_tokens))
    jaccard = overlap / len(left_tokens | right_tokens)
    return max(jaccard, containment)


def _title_fingerprint(value: str) -> str:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if token not in TITLE_STOPWORDS
    ]
    return " ".join(tokens)


def _mechanism_cluster(record: dict) -> str:
    text = " ".join([
        str(record.get("title") or ""),
        str(record.get("script_id") or ""),
        " ".join(str(item) for item in record.get("features") or ()),
        " ".join(str(item) for item in record.get("tags") or ()),
    ]).lower()
    checks = (
        ("orderflow", ("orderflow", "order flow", "footprint", "cvd", "delta", "absorption")),
        ("liquidity", ("liquidity", "sweep", "fvg", "fair value", "order block", "ob fvg")),
        ("structure", ("structure", "mss", "bos", "choch", "support", "resistance", "sr")),
        ("breakout", ("breakout", "bounce", "box", "range")),
        ("momentum", ("momentum", "rsi", "roc", "macd", "cascade", "stoch")),
        ("trend", ("trend", "ema", "supertrend", "ut", "trail")),
        ("volume", ("volume", "vwap", "mfi", "obv")),
        ("volatility", ("atr", "volatility", "bollinger", "bb", "keltner")),
        ("risk_plan", ("stop", "take", "tp", "sl", "risk")),
        ("mtf", ("mtf", "multi tf", "multi timeframe")),
    )
    for mechanism, needles in checks:
        if any(needle in text for needle in needles):
            return mechanism
    return "general"


def _next_action(record: dict) -> str:
    verdict = str(record.get("crypto_portability") or "")
    source_available = bool(record.get("source_available"))
    if verdict == "BLOCKED_REPAINT_RISK":
        return "RUN_CAUSALITY_AUDIT"
    if not source_available:
        return "REQUEST_OPEN_SOURCE_EXPORT"
    if verdict in {"PORTABLE", "PORTABLE_WITH_CHANGES"}:
        return "PORT_CAUSAL_FEATURES_AND_REPLAY"
    if verdict == "RESEARCH_ONLY":
        return "DISTILL_FEATURES_ONLY"
    return "MANUAL_REVIEW"


def _priority_score(record: dict, mechanism: str) -> int:
    verdict = str(record.get("crypto_portability") or "")
    risks = set(str(item) for item in record.get("risks") or ())
    score = _bounded_int(record.get("crypto_fit_score"), 0, 100)
    score += MECHANISM_PRIORITY.get(mechanism, 0)
    if record.get("source_available"):
        score += 18
    else:
        score -= 10
    if verdict in {"PORTABLE", "PORTABLE_WITH_CHANGES"}:
        score += 10
    elif verdict == "RESEARCH_ONLY":
        score -= 8
    elif verdict == "BLOCKED_REPAINT_RISK":
        score -= 45
    if "mtf_repaint_review_required" in risks:
        score -= 15
    if "visual_label_not_execution_strategy" in risks:
        score -= 8
    if "no_machine_alert_payload" in risks:
        score -= 4
    return max(0, min(100, score))


def _priority_reason(record: dict, mechanism: str, next_action: str) -> str:
    if record.get("source_available"):
        return (
            f"{mechanism} source is available; {next_action.lower().replace('_', ' ')} "
            "before any promotion."
        )
    return (
        f"{mechanism} catalog hit; source export is required before VNEDGE can "
        "port or backtest it."
    )


def _bounded_int(value: object, floor: int, ceiling: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return floor
    if not math.isfinite(parsed):
        return floor
    return max(floor, min(ceiling, parsed))


def _is_pine_source_path(path: Path) -> bool:
    return path.suffix.lower() in PINE_SOURCE_SUFFIXES


def _clean_url(url: str) -> str:
    return str(url or "").strip()


def _dedupe_urls(urls: Iterable[str], *, limit: int | None = None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        normalized = _normalize_tradingview_url(url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
        if limit is not None and len(out) >= limit:
            break
    return tuple(out)


def _dedupe_plain_urls(urls: Iterable[str], *, limit: int | None = None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        normalized = str(url or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
        if limit is not None and len(out) >= limit:
            break
    return tuple(out)


def _normalize_tradingview_url(
    url: str,
    *,
    base_url: str = TRADINGVIEW_BASE_URL,
) -> str:
    joined = urljoin(base_url, unescape(str(url or "").strip()))
    parsed = urlparse(joined)
    if "/script/" not in parsed.path:
        return ""
    path = parsed.path
    if not path.endswith("/"):
        path = f"{path}/"
    return f"https://www.tradingview.com{path}"


def _normalize_tradingview_catalog_url(
    url: str,
    *,
    base_url: str = TRADINGVIEW_BASE_URL,
) -> str:
    joined = urljoin(base_url, unescape(str(url or "").strip()))
    parsed = urlparse(joined)
    if parsed.netloc and not parsed.netloc.endswith("tradingview.com"):
        return ""
    if "/script/" in parsed.path or not parsed.path.startswith("/scripts/"):
        return ""
    path = parsed.path
    if not path.endswith("/"):
        path = f"{path}/"
    return f"https://www.tradingview.com{path}"


def _is_tradingview_script_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.endswith("tradingview.com") and parsed.path.startswith("/script/")


def _slug_from_script_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "script":
        return parts[1]
    return "tradingview-script"


def _script_id_from_url(url: str) -> str:
    return _script_id_from_path(Path(_slug_from_script_url(url)))


def _title_from_slug(slug: str) -> str:
    pieces = slug.split("-", 1)
    title_slug = pieces[1] if len(pieces) == 2 else pieces[0]
    title = re.sub(r"[-_]+", " ", title_slug).strip()
    return title.title() if title else "TradingView Script"


def _script_id_from_path(path: Path) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", path.stem.strip()).strip("_").lower()
    return cleaned or "pine_script"


def _title_from_source(source: str, fallback: str) -> str:
    match = re.search(
        r"\b(?:indicator|strategy|library)\s*\(\s*['\"]([^'\"]+)['\"]",
        source,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else fallback.replace("_", " ").strip()


def _author_from_source(source: str) -> str:
    for line in source.splitlines()[:20]:
        match = re.search(r"(?:author|created by)\s*[:=]\s*(.+)$", line, re.IGNORECASE)
        if match:
            return match.group(1).strip().strip("/ ")
    return "user supplied"


def _license_from_source(source: str) -> str:
    lower = source.lower()
    if "mozilla public license" in lower or "mpl-2.0" in lower:
        return "MPL-2.0"
    if "mit license" in lower:
        return "MIT"
    if "apache license" in lower:
        return "Apache-2.0"
    return "unknown"


def _detect_features(lower_source: str) -> set[str]:
    checks = {
        "trend": ("ema", "supertrend", "adx", "trend"),
        "breakout": ("breakout", "highest", "lowest", "box", "range"),
        "momentum": ("rsi", "roc", "macd", "momentum", "stoch"),
        "volatility": ("atr", "bb", "bollinger", "kc", "volatility"),
        "volume": ("volume", "vwap", "obv", "mfi"),
        "orderflow": ("order flow", "orderflow", "footprint", "cvd", "delta", "absorption"),
        "liquidity": ("liquidity", "sweep", "fvg", "fair value", "order block"),
        "structure": ("structure", "mss", "bos", "choch", "support", "resistance"),
        "mtf": ("request.security", "timeframe.", "security("),
        "risk_plan": ("stop", "sl", "take", "tp", "strategy.exit"),
        "alerts": ("alertcondition", "alert("),
    }
    return {
        name
        for name, needles in checks.items()
        if any(needle in lower_source for needle in needles)
    }


def _detect_risks(lower_source: str) -> set[str]:
    risks: set[str] = set()
    if "lookahead_on" in lower_source:
        risks.add("lookahead_on")
    if "request.security" in lower_source and "barstate.isconfirmed" not in lower_source:
        risks.add("mtf_repaint_review_required")
    if "plotshape" in lower_source and "strategy." not in lower_source:
        risks.add("visual_label_not_execution_strategy")
    if "barstate.islast" in lower_source:
        risks.add("last_bar_display_logic")
    if "alert(" not in lower_source and "alertcondition" not in lower_source:
        risks.add("no_machine_alert_payload")
    return risks


def _crypto_fit_score(features: set[str], risks: set[str], source: str) -> int:
    score = 35
    for feature in ("trend", "breakout", "momentum", "volatility", "volume", "risk_plan"):
        if feature in features:
            score += 8
    if "alerts" in features:
        score += 6
    if "mtf" in features:
        score += 4
    if "visual_label_not_execution_strategy" in risks:
        score -= 10
    if "mtf_repaint_review_required" in risks:
        score -= 18
    if "lookahead_on" in risks:
        score -= 35
    if len(source.splitlines()) > 900:
        score -= 5
    return max(0, min(100, score))


def _porting_notes(features: set[str], risks: set[str]) -> tuple[str, ...]:
    notes = []
    if "breakout" in features:
        notes.append("Port prior-bar range/level calculations with current bar excluded.")
    if "mtf" in features:
        notes.append("Resample higher timeframes causally and forward-fill only closed HTF bars.")
    if "risk_plan" in features:
        notes.append("Replace visual TP/SL lines with VNEDGE SignalIntent stop/target geometry.")
    if "volume" in features:
        notes.append("Normalize volume by venue/symbol because crypto perp volume scales differ.")
    if "visual_label_not_execution_strategy" in risks:
        notes.append("Treat chart labels as hypotheses; route only cost-clearing intents.")
    if not notes:
        notes.append("Needs manual mechanism extraction before a VNEDGE port.")
    return tuple(notes)


def _uplift_ideas(features: set[str], risks: set[str]) -> tuple[str, ...]:
    ideas = []
    if "trend" in features and "momentum" in features:
        ideas.append("Score trend/momentum agreement as an edge-model feature instead of a hard gate.")
    if "breakout" in features:
        ideas.append("Mine box width, participation, and room-to-liquidity thresholds by venue.")
    if "volatility" in features:
        ideas.append("Condition thresholds on volatility regime to avoid chop overfitting.")
    if "mtf_repaint_review_required" in risks:
        ideas.append("Add a causality audit before any backtest; reject if closed-bar parity fails.")
    if not ideas:
        ideas.append("Cluster against existing VNEDGE scanners and reject duplicates after cost.")
    return tuple(ideas)


def _policy() -> dict:
    return {
        "research_only": True,
        "can_trade": False,
        "can_promote": False,
        "source_rule": "review public/open-source/user-supplied Pine only; do not copy protected scripts",
        "promotion_rule": "port -> causality test -> multi-TF replay -> untouched judgment -> shadow/paper",
        "timeframes": list(DEFAULT_TIMEFRAMES),
        "venues": list(DEFAULT_VENUES),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="publish a research-only Pine script review KB artifact"
    )
    parser.add_argument("source_files", nargs="*", help="explicit Pine source files")
    parser.add_argument(
        "--source-dir",
        default=str(DEFAULT_PINE_SOURCE_DIR),
        help="directory scanned recursively for .pine/.pinescript/.txt files",
    )
    parser.add_argument("--output", default=str(DEFAULT_PINE_KB_PATH))
    parser.add_argument(
        "--catalog-url",
        action="append",
        default=[],
        help="TradingView catalog/profile/tag/script URL to discover as metadata-only backlog",
    )
    parser.add_argument(
        "--include-tradingview-discovery",
        action="store_true",
        help="crawl the built-in TradingView discovery preset as metadata-only backlog",
    )
    parser.add_argument(
        "--discovery-url",
        action="append",
        default=[],
        help="additional TradingView catalog/tag URL for the discovery frontier",
    )
    parser.add_argument(
        "--discovery-depth",
        type=int,
        default=0,
        help="catalog-link crawl depth for TradingView discovery URLs",
    )
    parser.add_argument(
        "--max-discovery-pages",
        type=int,
        default=40,
        help="maximum TradingView catalog/tag pages to fetch during discovery",
    )
    parser.add_argument(
        "--catalog-html",
        action="append",
        default=[],
        help="saved TradingView HTML file to parse for script URLs",
    )
    parser.add_argument(
        "--extraction-manifest",
        action="append",
        default=[],
        help=(
            "JSONL manifest from browser-based open-source Pine extraction; "
            "used for provenance/status only"
        ),
    )
    parser.add_argument(
        "--max-catalog-records",
        type=int,
        default=250,
        help="maximum discovered TradingView script URLs to add",
    )
    parser.add_argument(
        "--discovery-total",
        type=int,
        default=None,
        help=(
            "optional total records seen by an external/bulk crawler; used only "
            "for coverage auditing when the active KB is source-backed-only"
        ),
    )
    parser.add_argument(
        "--no-defaults",
        action="store_true",
        help="do not include built-in seed records",
    )
    parser.add_argument("--json", action="store_true", help="print full payload")
    args = parser.parse_args(argv)

    payload = publish_pine_research_kb(
        source_dir=args.source_dir,
        source_files=args.source_files,
        catalog_urls=args.catalog_url,
        catalog_discovery_urls=(
            [*DEFAULT_TRADINGVIEW_DISCOVERY_URLS] if args.include_tradingview_discovery else []
        )
        + list(args.discovery_url),
        catalog_html_files=args.catalog_html,
        extraction_manifest_files=args.extraction_manifest,
        output_path=args.output,
        include_defaults=not args.no_defaults,
        max_catalog_records=max(0, args.max_catalog_records),
        catalog_discovery_depth=max(0, args.discovery_depth),
        max_catalog_pages=max(0, args.max_discovery_pages),
        discovered_total=args.discovery_total,
        source_label="pine_research_publisher",
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        summary = payload["summary"]
        print(
            "pine research KB published: "
            f"total={summary['total']} portable={summary['portable']} "
            f"source_backed={summary['source_backed']} "
            f"catalog_only={summary['catalog_only']} "
            f"needs_source={summary['needs_source']} "
            f"port_queue={summary['port_queue']} "
            f"queued={summary['backtests_queued']} output={args.output}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
