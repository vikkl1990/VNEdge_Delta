"""AI strategy sandbox — security matrix + candidate-research wiring.

Two things are proven here:

1. DENY-BY-DEFAULT. ``validate_strategy_source`` / ``load_ai_strategy`` must
   reject every classic escape and lookahead primitive: dangerous imports,
   ``eval``/``exec``/``compile``/``open``/``__import__``, dunder attribute
   reflection, negative-shift and future-index lookahead, and any source
   without a well-formed ``BaseStrategy`` subclass. The restricted exec
   namespace blocks escapes even if the AST layer were bypassed. The safe
   example strategy loads, runs, and passes the causality analyzer, and its
   ``strategy_id`` is force-namespaced ``ai_`` and never auto-registered.

2. The candidate-research surface (``ai_candidate_research``) causality-checks
   and walk-forwards AI strategies, REFUSES lookahead the AST can't see (a
   feature that isn't truncation-invariant), tolerates malformed files, and
   stamps ``can_trade=False`` / ``can_promote=False`` /
   ``requires_untouched_judgment=True`` on everything.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from vnedge.research.ai_candidate_research import (
    VERDICT_INSUFFICIENT_DATA,
    VERDICT_REFUSED_LOOKAHEAD,
    ai_candidate_policy,
    run_ai_candidate_research,
    write_ai_candidates_payload,
)
from vnedge.research.causality_analyzer import analyze_strategy, synthetic_market
from vnedge.strategy.ai_sandbox import (
    AI_STRATEGY_ID_PREFIX,
    SandboxViolation,
    ValidationReport,
    _guarded_import,
    _restricted_globals,
    _safe_builtins,
    ai_strategies_from_dir,
    load_ai_strategy,
    validate_strategy_source,
)
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.strategy_registry import STRATEGIES

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = REPO_ROOT / "data" / "strategies" / "ai" / "example_ma_cross_ai.py"
CANDLES, FUNDING = synthetic_market()


# --------------------------------------------------------------------------
# Source builders
# --------------------------------------------------------------------------


def _wrap(
    *,
    header: str = "",
    prepare_body: str = "        return candles.copy()\n",
    signal_body: str = "        return None\n",
    strategy_id: str = '"t"',
) -> str:
    """A minimal but VALID BaseStrategy subclass, with hooks to inject a single
    offending construct so the resulting violation is unambiguous."""
    return (
        "from __future__ import annotations\n"
        "import pandas as pd\n"
        "from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent\n"
        f"{header}"
        "\n"
        "class S(BaseStrategy):\n"
        f"    strategy_id = {strategy_id}\n"
        "    warmup_bars = 2\n"
        "    def prepare(self, candles):\n"
        f"{prepare_body}"
        "    def signal(self, df, index):\n"
        f"{signal_body}"
    )


def test_baseline_template_is_accepted() -> None:
    """Canary: the un-injected template MUST validate — otherwise the deny
    matrix below would 'pass' for the wrong reason (rejecting valid code)."""
    report = validate_strategy_source(_wrap())
    assert report.ok, report.describe()
    assert report.strategy_class_name == "S"


# --------------------------------------------------------------------------
# DENY-BY-DEFAULT matrix
# --------------------------------------------------------------------------

_DENY_CASES: list[tuple[str, str, str]] = [
    # dangerous imports
    ("import_os", _wrap(header="import os\n"), "os"),
    ("import_sys", _wrap(header="import sys\n"), "sys"),
    ("import_subprocess", _wrap(header="import subprocess\n"), "subprocess"),
    ("import_socket", _wrap(header="import socket\n"), "socket"),
    ("from_pathlib_import_Path", _wrap(header="from pathlib import Path\n"), "pathlib"),
    # dangerous builtins (calls)
    ("eval", _wrap(signal_body='        return eval("1")\n'), "eval"),
    ("exec", _wrap(signal_body='        exec("x = 1")\n        return None\n'), "exec"),
    ("compile", _wrap(signal_body='        compile("1", "<s>", "eval")\n        return None\n'), "compile"),
    ("dunder_import_call", _wrap(signal_body='        return __import__("os")\n'), "__import__"),
    ("open", _wrap(signal_body='        return open("/etc/passwd")\n'), "open"),
    # dunder attribute reflection (classic sandbox escapes)
    ("dunder_globals", _wrap(signal_body="        return self.signal.__globals__\n"), "__globals__"),
    ("dunder_builtins", _wrap(signal_body="        return df.__builtins__\n"), "__builtins__"),
    ("dunder_class", _wrap(signal_body="        return df.__class__\n"), "__class__"),
    ("dunder_mro", _wrap(signal_body="        return S.__mro__\n"), "__mro__"),
    ("dunder_subclasses", _wrap(signal_body="        return object.__subclasses__()\n"), "__subclasses__"),
    ("escape_bases", _wrap(signal_body="        return ().__class__.__bases__\n"), "__class__"),
    ("escape_type_subclasses", _wrap(signal_body="        return type.__subclasses__(object)\n"), "__subclasses__"),
    # lookahead: negative shift reads future rows into the past
    (
        "negative_shift",
        _wrap(
            prepare_body=(
                "        df = candles.copy()\n"
                '        df["y"] = df["close"].shift(-1)\n'
                "        return df\n"
            )
        ),
        "shift",
    ),
    (
        "negative_shift_kw",
        _wrap(
            prepare_body=(
                "        df = candles.copy()\n"
                '        df["y"] = df["close"].shift(periods=-2)\n'
                "        return df\n"
            )
        ),
        "shift",
    ),
    # lookahead: future positional index
    ("iloc_index_plus_1", _wrap(signal_body="        return df.iloc[index + 1]\n"), "future row"),
    ("iloc_i_plus_1", _wrap(signal_body="        i = index\n        return df.iloc[i + 1]\n"), "future row"),
    ("loc_index_plus_2", _wrap(signal_body="        return df.loc[index + 2]\n"), "future row"),
    # forbidden bare names
    ("bare_os_name", _wrap(signal_body="        return os\n"), "os"),
]


@pytest.mark.parametrize("label,source,needle", _DENY_CASES, ids=[c[0] for c in _DENY_CASES])
def test_validator_denies(label: str, source: str, needle: str) -> None:
    report = validate_strategy_source(source)
    assert not report.ok, f"{label} should have been REJECTED but validated ok"
    blob = report.describe()
    assert needle in blob, f"{label}: expected reason containing {needle!r} in:\n{blob}"
    # load_ai_strategy must refuse to even exec a source with any violation.
    with pytest.raises(SandboxViolation):
        load_ai_strategy(source)


def test_no_base_strategy_subclass_is_rejected() -> None:
    src = (
        "from __future__ import annotations\n"
        "def helper():\n"
        "    return 1\n"
    )
    report = validate_strategy_source(src)
    assert not report.ok
    assert "no BaseStrategy subclass" in report.describe()
    with pytest.raises(SandboxViolation):
        load_ai_strategy(src)


def test_two_base_strategy_subclasses_is_rejected() -> None:
    src = _wrap() + (
        "\nclass S2(BaseStrategy):\n"
        '    strategy_id = "t2"\n'
        "    def prepare(self, candles):\n"
        "        return candles.copy()\n"
        "    def signal(self, df, index):\n"
        "        return None\n"
    )
    report = validate_strategy_source(src)
    assert not report.ok
    assert "exactly one BaseStrategy subclass" in report.describe()


def test_missing_prepare_or_signal_is_rejected() -> None:
    src = (
        "from __future__ import annotations\n"
        "from vnedge.strategy.base_strategy import BaseStrategy\n"
        "\nclass S(BaseStrategy):\n"
        '    strategy_id = "t"\n'
        "    def prepare(self, candles):\n"
        "        return candles.copy()\n"
    )
    report = validate_strategy_source(src)
    assert not report.ok
    assert "missing signal" in report.describe()


def test_syntax_error_is_a_clean_violation_not_a_crash() -> None:
    report = validate_strategy_source("class S(:\n  pass\n")
    assert not report.ok
    assert report.violations[0].node_type == "SyntaxError"


# --------------------------------------------------------------------------
# Causal lookahead patterns are only rejected in the future direction
# --------------------------------------------------------------------------


def test_backward_shift_and_current_index_are_allowed() -> None:
    """The causal mirror of the rejected patterns must be ACCEPTED — the
    validator distinguishes future access from past access."""
    ok_src = _wrap(
        prepare_body=(
            "        df = candles.copy()\n"
            '        df["prev"] = df["close"].shift(1)\n'      # backward: fine
            "        return df\n"
        ),
        signal_body="        return df.iloc[index - 1]\n",       # past row: fine
    )
    assert validate_strategy_source(ok_src).ok, validate_strategy_source(ok_src).describe()


# --------------------------------------------------------------------------
# Restricted execution: builtins subset + runtime import guard
# --------------------------------------------------------------------------


def test_safe_builtins_exclude_escape_primitives() -> None:
    builtins = _safe_builtins()
    for banned in ("open", "eval", "exec", "compile", "getattr", "setattr", "globals", "vars"):
        assert banned not in builtins, f"{banned} must not be exposed to AI source"
    # __import__ is present but is the GUARD, not the real one.
    assert builtins["__import__"] is _guarded_import
    # a couple of genuinely needed safe builtins remain
    assert "range" in builtins and "len" in builtins


def test_guarded_import_blocks_os_but_allows_pandas() -> None:
    with pytest.raises(SandboxViolation):
        _guarded_import("os")
    with pytest.raises(SandboxViolation):
        _guarded_import("subprocess")
    # whitelisted roots still import
    assert _guarded_import("math") is __import__("math")


def test_restricted_namespace_blocks_runtime_import_escape() -> None:
    """Even if a bad import slipped past the AST layer, executing it in the
    restricted namespace must fail closed."""
    ns = _restricted_globals("ai_escape")
    with pytest.raises(SandboxViolation):
        exec("__import__('os').system('echo pwned')", ns)  # noqa: S102 - the point


# --------------------------------------------------------------------------
# The safe example: loads, runs, is causal, namespaced, unregistered
# --------------------------------------------------------------------------


def test_example_validates() -> None:
    report = validate_strategy_source(EXAMPLE_PATH.read_text())
    assert isinstance(report, ValidationReport)
    assert report.ok, report.describe()
    assert report.strategy_id == "example_ma_cross"


def test_example_loads_and_is_forced_ai_prefixed() -> None:
    cls = load_ai_strategy(EXAMPLE_PATH.read_text())
    assert issubclass(cls, BaseStrategy)
    assert cls.strategy_id == "ai_example_ma_cross"
    assert cls.strategy_id.startswith(AI_STRATEGY_ID_PREFIX)


def test_already_prefixed_id_is_not_double_prefixed() -> None:
    src = _wrap(strategy_id='"ai_already"')
    cls = load_ai_strategy(src)
    assert cls.strategy_id == "ai_already"


def test_example_runs_prepare_and_signal_on_synthetic_candles() -> None:
    cls = load_ai_strategy(EXAMPLE_PATH.read_text())
    strat = cls()
    df = strat.prepare(CANDLES)
    assert len(df) == len(CANDLES)
    assert CANDLES is not df  # prepare() must not mutate the input frame
    # warmup rows produce no signal; past warmup a signal is None or a valid intent
    assert strat.signal(df, 0) is None
    fired = 0
    for i in range(strat.warmup_bars, len(df)):
        intent = strat.signal(df, i)
        if intent is not None:
            assert isinstance(intent, SignalIntent)
            assert intent.stop_price > 0
            fired += 1
    assert fired > 0, "the example must actually fire on the synthetic market"


def test_example_passes_causality_analyzer() -> None:
    cls = load_ai_strategy(EXAMPLE_PATH.read_text())

    def factory(funding=None, **params):
        return cls(**params)

    report = analyze_strategy(factory, CANDLES, FUNDING)
    assert report.passed, report.describe()
    assert report.signal_indexes_checked > 0
    assert report.fired_bars > 0


def test_ai_strategy_is_never_auto_registered() -> None:
    cls = load_ai_strategy(EXAMPLE_PATH.read_text())
    assert cls.strategy_id not in STRATEGIES
    # nothing ai_* may live in the trading registry
    assert not any(sid.startswith(AI_STRATEGY_ID_PREFIX) for sid in STRATEGIES)


# --------------------------------------------------------------------------
# ai_strategies_from_dir: bad files are skipped, not raised, and logged
# --------------------------------------------------------------------------


def test_from_dir_skips_invalid_file_without_raising(tmp_path, caplog) -> None:
    (tmp_path / "good.py").write_text(_wrap(strategy_id='"good"'))
    (tmp_path / "evil.py").write_text(_wrap(strategy_id='"evil"', header="import os\n"))
    (tmp_path / "broken.py").write_text("class S(:\n  pass\n")  # syntax error

    with caplog.at_level(logging.WARNING, logger="vnedge.strategy.ai_sandbox"):
        loaded = ai_strategies_from_dir(tmp_path)

    assert set(loaded) == {"ai_good"}  # only the valid one
    blob = caplog.text
    assert "evil.py" in blob and "broken.py" in blob
    assert "rejected" in blob or "could not parse" in blob


def test_from_dir_absent_directory_returns_empty(tmp_path) -> None:
    assert ai_strategies_from_dir(tmp_path / "nope") == {}


# --------------------------------------------------------------------------
# Candidate research surface
# --------------------------------------------------------------------------


def _write_leak_strategy(directory: Path) -> None:
    """A strategy that PASSES the AST validator (no shift/negative-index) but
    is NOT truncation-invariant: it stamps the final bar's close onto every
    row via ``.iloc[-1]`` — a leak only the causality analyzer catches."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "leak.py").write_text(
        "from __future__ import annotations\n"
        "import pandas as pd\n"
        "from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent\n"
        "\nclass LeakAI(BaseStrategy):\n"
        '    strategy_id = "leak"\n'
        "    warmup_bars = 2\n"
        "    def prepare(self, candles):\n"
        "        df = candles.copy()\n"
        '        df["last_close"] = df["close"].iloc[-1]\n'  # leaks the future
        "        return df\n"
        "    def signal(self, df, index):\n"
        "        row = df.iloc[index]\n"
        '        if float(row["close"]) > float(row["last_close"]):\n'
        '            return SignalIntent(side="long", stop_price=float(row["close"]) * 0.99)\n'
        "        return None\n"
    )


def test_leak_strategy_passes_ast_but_is_refused_by_causality(tmp_path) -> None:
    _write_leak_strategy(tmp_path)
    # It DOES validate at the AST layer (the point — AST can't see this leak).
    assert validate_strategy_source((tmp_path / "leak.py").read_text()).ok

    payload = run_ai_candidate_research(
        CANDLES, FUNDING, strategy_dir=tmp_path, train_bars=200, test_bars=100
    )
    assert len(payload["candidates"]) == 1
    entry = payload["candidates"][0]
    assert entry["strategy_id"] == "ai_leak"
    assert entry["verdict"] == VERDICT_REFUSED_LOOKAHEAD
    assert entry["causality"]["passed"] is False
    assert entry["causality"]["violations"]  # concrete evidence attached
    assert entry["walk_forward"] is None  # no walk-forward on a non-causal strategy


def test_candidate_research_on_example_is_causal_and_guarded(tmp_path) -> None:
    src = EXAMPLE_PATH.read_text()
    (tmp_path / "example.py").write_text(src)
    # default (1440/720) bars can't fit the ~456-bar synthetic market — the
    # candidate lands as INSUFFICIENT_DATA, still causal, never a spurious pass.
    payload = run_ai_candidate_research(CANDLES, FUNDING, strategy_dir=tmp_path)
    entry = payload["candidates"][0]
    assert entry["strategy_id"] == "ai_example_ma_cross"
    assert entry["causality"]["passed"] is True
    assert entry["verdict"] == VERDICT_INSUFFICIENT_DATA
    # hard governance guards on every layer of the payload
    for scope in (payload, payload["summary"], entry, payload["policy"]):
        assert scope["can_trade"] is False
        assert scope["can_promote"] is False
        assert scope["requires_untouched_judgment"] is True


def test_candidate_research_reports_rejected_files_without_crashing(tmp_path) -> None:
    (tmp_path / "evil.py").write_text(_wrap(strategy_id='"evil"', header="import subprocess\n"))
    (tmp_path / "noclass.py").write_text("x = 1\n")
    payload = run_ai_candidate_research(CANDLES, FUNDING, strategy_dir=tmp_path)
    assert payload["candidates"] == []
    rejected = {r["file"] for r in payload["rejected_files"]}
    assert rejected == {"evil.py", "noclass.py"}
    assert payload["summary"]["rejected_files"] == 2


def test_candidate_research_empty_dir_is_structured(tmp_path) -> None:
    payload = run_ai_candidate_research(CANDLES, FUNDING, strategy_dir=tmp_path / "empty")
    assert payload["candidates"] == []
    assert payload["rejected_files"] == []
    assert payload["policy"]["family"] == "ai_authored"


def test_write_ai_candidates_payload_is_atomic(tmp_path) -> None:
    payload = {"policy": ai_candidate_policy(), "candidates": []}
    path = write_ai_candidates_payload(payload, tmp_path)
    assert path.name == "ai_candidates.json"
    assert not list(tmp_path.glob("*.tmp"))  # no temp file left behind


# --------------------------------------------------------------------------
# Folding hook into the continuous research document
# --------------------------------------------------------------------------


def test_folding_hook_into_continuous_research(tmp_path, monkeypatch) -> None:
    import json

    import vnedge.research.continuous_research as cr

    out_dir = tmp_path / "live_research"
    monkeypatch.setattr(cr, "OUT_DIR", out_dir)
    assert cr._load_ai_candidates_latest() == {}  # absent -> {}

    payload = run_ai_candidate_research(
        CANDLES, FUNDING, strategy_dir=EXAMPLE_PATH.parent, train_bars=200, test_bars=100
    )
    write_ai_candidates_payload(payload, out_dir)
    assert cr._load_ai_candidates_latest()["policy"]["family"] == "ai_authored"

    cr.publish(cr.ResearchPayload(started=0.0, ai_candidates=cr._load_ai_candidates_latest()))
    latest = json.loads((out_dir / "latest.json").read_text())
    assert latest["ai_candidates"]["policy"]["can_trade"] is False
    assert latest["ai_candidates"]["policy"]["requires_untouched_judgment"] is True

    (out_dir / "ai_candidates.json").write_text("{corrupt")
    assert cr._load_ai_candidates_latest() == {}  # unreadable -> {}
