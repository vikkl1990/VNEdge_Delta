from __future__ import annotations

from datetime import UTC, datetime

from vnedge.research.quantified_strategy_lab import (
    SOURCE_POLICY,
    STRATEGY_SEEDS,
    build_quantified_strategy_lab_payload,
    load_quantified_strategy_lab_payload,
    publish_quantified_strategy_lab,
)


def _payload() -> dict:
    return build_quantified_strategy_lab_payload(
        generated_at=datetime(2026, 7, 30, tzinfo=UTC)
    )


def test_quantified_strategy_lab_tracks_all_95_titles() -> None:
    payload = _payload()

    assert len(STRATEGY_SEEDS) == 95
    assert payload["summary"]["total_strategies"] == 95
    assert len(payload["strategy_reviews"]) == 95
    assert payload["source"]["source_policy"] == SOURCE_POLICY
    assert payload["summary"]["source_backed_rules"] == 0
    assert payload["can_trade"] is False
    assert payload["can_promote"] is False


def test_bitcoin_strategy_is_fast_track_crypto_native() -> None:
    payload = _payload()

    row = next(r for r in payload["strategy_reviews"] if r["strategy_number"] == 48)

    assert row["crypto_portability"] == "PORTABLE_CRYPTO_NATIVE"
    assert row["recommended_port"] == "bitcoin_crypto_strategy_pack_v1"
    assert row["crypto_fit_score"] >= 90
    assert list(row["replay_timeframes"]) == ["1m", "5m", "15m", "1h", "4h"]
    assert list(row["replay_venues"]) == ["binanceusdm", "bybit", "delta_india"]


def test_overnight_titles_become_crypto_session_studies_not_copied_rules() -> None:
    payload = _payload()

    row = next(r for r in payload["strategy_reviews"] if r["strategy_number"] == 95)

    assert row["crypto_portability"] == "PORTABLE_AS_CRYPTO_SESSION_STUDY"
    assert row["recommended_port"] == "crypto_session_calendar_miner_v1"
    assert row["source_policy"] == SOURCE_POLICY
    assert row["can_trade"] is False


def test_golden_cross_is_not_misclassified_as_gold_asset_scope() -> None:
    payload = _payload()

    row = next(r for r in payload["strategy_reviews"] if r["strategy_number"] == 62)

    assert row["mechanism"] == "trend_momentum"
    assert row["asset_scope"] == "equity_index"
    assert row["recommended_port"] == "trend_momentum_pack_v1"


def test_coming_rows_are_blocked_placeholders() -> None:
    payload = _payload()

    rows = [r for r in payload["strategy_reviews"] if r["title"] == "Coming"]

    assert len(rows) == 6
    assert {r["crypto_fit_score"] for r in rows} == {0}
    assert {r["crypto_portability"] for r in rows} == {"PLACEHOLDER_NO_RULES"}
    assert all(not r["replay_timeframes"] for r in rows)


def test_port_tasks_group_replay_contracts() -> None:
    payload = _payload()
    ports = {task["recommended_port"]: task for task in payload["port_tasks"]}

    assert "pullback_reversion_pack_v1" in ports
    assert "indicator_pack_mtf_v1" in ports
    assert "crypto_session_calendar_miner_v1" in ports
    assert "expected net edge >25 bps after fees and slippage" in payload[
        "replay_contract"
    ]["promotion_gate"]
    assert all(task["can_trade"] is False for task in payload["port_tasks"])


def test_publish_and_load_round_trip(tmp_path) -> None:
    out = tmp_path / "quantified_strategy_lab_latest.json"
    feed = tmp_path / "quantified_strategy_lab_feed.jsonl"

    written = publish_quantified_strategy_lab(
        out=out,
        feed=feed,
        generated_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    loaded = load_quantified_strategy_lab_payload(out)

    assert loaded["lab_id"] == written["lab_id"]
    assert loaded["summary"]["total_strategies"] == 95
    assert feed.exists()
    assert "quantified_strategy_lab_v1" in feed.read_text(encoding="utf-8")
