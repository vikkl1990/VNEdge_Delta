# AI strategy sandbox

`vnedge.strategy.ai_sandbox` + `vnedge.research.ai_candidate_research` — the
safe path for **AI-authored** trading strategies. The whole point is to capture
the *generation* leverage of "let a model write a strategy" **without ever
letting AI-authored code drive execution, touch the network or filesystem, or
reach the core sources.**

An AI strategy is treated as exactly one thing: a research **candidate**. It
must clear the same causality analyzer, the same walk-forward promotion gates,
the same pre-registered untouched-data judgment, and the same human approval as
every hand-written strategy. Nothing in this subsystem trades, promotes, or
auto-registers anything.

## How this differs from "Vibe-Trading"

The popular pattern lets an LLM *drive*: generate signals and act on them, or
even place orders, in a loop. We deliberately reject AI-driven **execution**.
We adopt AI **generation**: the model authors (or reviews) a strategy as source
code, and that code enters the same research funnel as any other candidate. The
model never trades directly; it never sees keys; its output is data that gets
validated, causality-checked, backtested, judged, and human-approved before a
single order exists. Generation is leverage; execution stays gated.

## Two layers of defense

### 1. Deny-by-default AST validation — `validate_strategy_source`

The source is parsed (never executed) and **every AST node is checked against an
allowlist**. Anything not positively recognised is a violation. On top of the
node allowlist:

- **Imports** must be on a tiny whitelist: `pandas`, `numpy`, `math`,
  `dataclasses`, and the two curated modules `vnedge.strategy.indicators` /
  `vnedge.strategy.base_strategy`. `vnedge` is *not* an importable root — an AI
  strategy can never reach data/network/execution code. Everything else —
  `os`, `sys`, `subprocess`, `socket`, `pathlib`, `importlib`, `ctypes`,
  `pickle`, `requests`, … — is rejected.
- **Dangerous builtins** cannot be called: `eval`, `exec`, `compile`,
  `__import__`, `open`, plus reflection primitives (`getattr`/`setattr`/`vars`/
  `globals`/`locals`/…).
- **Dunder reflection** is blocked: any `__globals__` / `__builtins__` /
  `__class__` / `__mro__` / `__subclasses__` / `__bases__` attribute access or
  bare dunder name — the classic `().__class__.__bases__` /
  `type.__subclasses__(...)` escape chains.
- **Lookahead patterns** are rejected structurally: `shift(-n)` (negative
  period) and future positional indexing `iloc[index + k]` / `loc[i + 1]`. The
  causal mirrors (`shift(1)`, `iloc[index - 1]`) are allowed.
- **Structure**: exactly one `BaseStrategy` subclass, with `prepare` + `signal`
  and a string `strategy_id`. Zero subclasses, two subclasses, or a missing
  method all reject.

The validator returns a `ValidationReport` listing **every** violation (not just
the first), so a rejection is explainable.

### 2. Restricted execution — `load_ai_strategy`

Validation runs first (raising `SandboxViolation` on any violation). Only then
is the already-validated source `exec`'d in a **restricted namespace**:

- `__builtins__` is a small safe subset — **no** `open`/`eval`/`exec`/`compile`/
  `getattr`/`globals`.
- `__import__` is replaced by a guard (`_guarded_import`) that admits only the
  whitelisted modules and blocks relative imports — defense-in-depth, so even a
  pattern that somehow slipped past the AST cannot import `os` at runtime.
- The loaded class's `strategy_id` is **force-prefixed `ai_`**, so an AI
  strategy is always distinguishable from a hand-written / registered one.

`ai_strategies_from_dir` loads a whole directory, **skipping (with a logged
reason)** any file that fails validation or load — a malformed AI strategy can
never crash the loader or the research loop.

## Research surface — `ai_candidate_research`

`python -m vnedge.research.ai_candidate_research`

For every file under `data/strategies/ai/`:

1. **Validate + load** (the two layers above). Rejects are counted into
   `rejected_files`, never fatal.
2. **Causality gate** — `analyze_strategy` must prove truncation invariance.
   **Fail closed:** any violation, or any error trying to verify it, sets the
   verdict to `REFUSED_LOOKAHEAD` / `REFUSED_CAUSALITY_ERROR` and **no
   walk-forward runs.** This catches leaks the AST cannot see (e.g. a feature
   built from `df["close"].iloc[-1]`, valid syntax but not causal).
3. **Walk-forward + frozen promotion gates.** A passing result is a
   `CANDIDATE`; a causal-but-failing one is a `REJECT`; too little data is
   `INSUFFICIENT_DATA`.

The payload is published to `research/live_research/ai_candidates.json` and
folded into the continuous-research document
(`vnedge.research.continuous_research`) exactly like the cascade-reversion hook,
so it runs on the existing research cadence — **no new always-on service.**
Toggle with `AI_CANDIDATE_RESEARCH_ENABLED=0`.

### Hard guards (stamped on the payload, summary, policy, and every row)

```
can_trade = False
can_promote = False
requires_untouched_judgment = True
auto_registered = False
```

AI strategies are namespaced `ai_*` and are **never** inserted into
`strategy_registry.STRATEGIES`. There is no code path by which an AI-authored
strategy reaches the order pipeline without going through pre-registered
untouched-data judgment and explicit human approval — the same promotion
machinery every strategy uses.

## The promotion ladder for an AI strategy

```
author (AI)  ->  AST validate + restricted load  ->  causality gate (REFUSE on lookahead)
             ->  rolling walk-forward + promotion gates  =>  CANDIDATE
             ->  pre-registered judgment on UNTOUCHED data  ->  human approval
             ->  paper -> shadow -> live_small -> live_full   (unchanged mode ladder)
```

A `CANDIDATE` verdict is a *prompt* for the next step, never the step itself.

## Tests

`tests/test_ai_sandbox.py` is the security matrix: it proves each dangerous
import, `eval`/`exec`/`compile`/`open`/`__import__`, dunder-reflection escape,
negative-shift and future-index lookahead, and the no-subclass case are all
rejected; that the restricted namespace blocks a runtime import escape; that the
safe example loads, runs, passes the causality analyzer, is force-`ai_`-prefixed,
and is absent from the trading registry; and that the candidate-research surface
REFUSES a lookahead only causality can see, tolerates malformed files, and
carries the guards above.

## Writing an AI strategy

Follow `docs/strategy_contract.md` (the behavioural contract is identical for
human and AI authors) and stay inside the whitelist. A minimal, safe, passing
example lives at `data/strategies/ai/example_ma_cross_ai.py`.
