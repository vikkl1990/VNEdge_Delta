"""Pre-live checklist — fail-closed gating, explainable failures."""

from vnedge.config.risk_config import RiskConfig
from vnedge.config.settings import Settings, TradingMode
from vnedge.runtime.pre_live_checklist import run_pre_live_checklist


def _live_settings(**kw):
    base = dict(
        trading_mode=TradingMode.LIVE_SMALL,
        live_trading_enabled=True,
        confirm_live_trading="I_UNDERSTAND_THIS_IS_HIGH_RISK",
        live_small_capital_cap_usd=200.0,
    )
    base.update(kw)
    return Settings(**base)


def _run(settings, tmp_path, **overrides):
    kw = dict(
        settings=settings,
        risk_config=settings.risk,
        kill_switch_active=False,
        has_unresolved_orders=False,
        journal_path=tmp_path / "journal.jsonl",
        credentials_present=True,
        lower_rungs_validated=True,
    )
    kw.update(overrides)
    return run_pre_live_checklist(**kw)


def test_all_green_clears(tmp_path):
    report = _run(_live_settings(), tmp_path)
    assert report.cleared
    assert report.failures == ()


def test_missing_a_gate_blocks(tmp_path):
    # phrase wrong -> is_live False -> three_live_gates fails -> blocked
    s = _live_settings(confirm_live_trading="nope")
    report = _run(s, tmp_path)
    assert not report.cleared
    assert any(f.name == "three_live_gates" for f in report.failures)


def test_kill_switch_blocks(tmp_path):
    report = _run(_live_settings(), tmp_path, kill_switch_active=True)
    assert not report.cleared
    assert any(f.name == "kill_switch_clear" for f in report.failures)


def test_missing_credentials_blocks(tmp_path):
    report = _run(_live_settings(), tmp_path, credentials_present=False)
    assert not report.cleared
    assert any(f.name == "trade_credentials_present" for f in report.failures)


def test_unresolved_orders_block(tmp_path):
    report = _run(_live_settings(), tmp_path, has_unresolved_orders=True)
    assert not report.cleared
    assert any(f.name == "reconciliation_clean" for f in report.failures)


def test_required_private_stream_freshness_blocks_when_stale(tmp_path):
    report = _run(
        _live_settings(),
        tmp_path,
        private_stream_required=True,
        private_stream_connected=False,
        private_stream_age_seconds=None,
    )
    assert not report.cleared
    assert any(f.name == "private_stream_fresh" for f in report.failures)


def test_required_private_stream_freshness_passes_when_connected(tmp_path):
    report = _run(
        _live_settings(),
        tmp_path,
        private_stream_required=True,
        private_stream_connected=True,
        private_stream_age_seconds=0.2,
    )
    check = next(c for c in report.results if c.name == "private_stream_fresh")
    assert report.cleared
    assert check.passed


def test_unvalidated_ladder_blocks(tmp_path):
    report = _run(_live_settings(), tmp_path, lower_rungs_validated=False)
    assert not report.cleared
    assert any(f.name == "mode_ladder_validated" for f in report.failures)


def test_live_small_capital_cap_present_and_enforced_upstream(tmp_path):
    import pytest
    # Settings itself rejects a non-positive cap (defence in depth upstream)
    with pytest.raises(Exception):
        _live_settings(live_small_capital_cap_usd=0.0)
    # and the checklist surfaces the cap for a valid live_small config
    report = _run(_live_settings(), tmp_path)
    cap = next(c for c in report.results if c.name == "live_small_capital_cap")
    assert cap.passed and "200" in cap.detail


def test_report_lists_every_failure_not_just_first(tmp_path):
    s = _live_settings(confirm_live_trading="nope")
    report = _run(s, tmp_path, kill_switch_active=True, credentials_present=False)
    failed = {f.name for f in report.failures}
    assert {"three_live_gates", "kill_switch_clear", "trade_credentials_present"} <= failed


def test_risk_config_is_frozen_and_within_leverage_cap(tmp_path):
    report = _run(_live_settings(), tmp_path)
    risk = next(c for c in report.results if c.name == "risk_config_frozen_valid")
    assert risk.passed
    assert "frozen=True" in risk.detail
    # sanity: RiskConfig really is frozen (defence in depth)
    assert RiskConfig.model_config.get("frozen") is True
