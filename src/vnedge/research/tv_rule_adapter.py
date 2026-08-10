"""Safe, local Pine-to-VNEDGE research rule adapter.

This module deliberately does *not* connect to TradingView or the unofficial
``tvscreener`` endpoints.  It accepts Pine source that is already lawfully
available to the operator, compiles a small auditable subset into an immutable
rule specification, and evaluates that specification on local closed-candle
OHLCV data.

The adapter is not a Pine virtual machine.  Unsupported language features fail
closed and no output from this module can trade or promote a strategy.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable, Literal, Sequence

import numpy as np
import pandas as pd


TV_RULE_ADAPTER_ID = "tv_rule_adapter_v1"
DEFAULT_OUT = Path("research/live_research/tv_rule_adapter_latest.json")
ALLOWED_LICENSES = frozenset(
    {
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "MIT",
        "MPL-2.0",
        "public-domain",
        "user-supplied",
    }
)
SUPPORTED_FUNCTIONS = frozenset(
    {
        "input.bool",
        "input.float",
        "input.int",
        "input.source",
        "math.abs",
        "math.max",
        "math.min",
        "ta.atr",
        "ta.crossover",
        "ta.crossunder",
        "ta.ema",
        "ta.highest",
        "ta.lowest",
        "ta.roc",
        "ta.rsi",
        "ta.sma",
        "ta.stdev",
    }
)
REPAINT_BLOCKERS: dict[str, str] = {
    "lookahead_on": r"\bbarmerge\.lookahead_on\b|\blookahead_on\b",
    "realtime_only_state": r"\bbarstate\.isrealtime\b",
    "intrabar_persistent_state": r"\bvarip\b",
    "future_bar_reference": r"\[[ \t]*-[0-9]+[ \t]*\]",
}
UNSUPPORTED_PATTERNS: dict[str, str] = {
    "multi_timeframe_request": r"\brequest\.(?:security|security_lower_tf)\s*\(",
    "custom_function": r"=>",
    "mutable_reassignment": r":=",
    "array_or_matrix_state": r"\b(?:array|matrix|map)\.",
    "loop_or_switch": r"(?m)^\s*(?:for|while|switch)\b",
    "unsupported_order_lifecycle": r"\bstrategy\.(?:order|close|close_all|cancel|cancel_all)\s*\(",
}


@dataclass(frozen=True)
class TVAssignmentSpec:
    name: str
    expression: str
    role: Literal["input", "indicator", "derived"]


@dataclass(frozen=True)
class TVExitSpec:
    stop_expression: str | None = None
    limit_expression: str | None = None
    loss_expression: str | None = None
    profit_expression: str | None = None
    trail_expression: str | None = None


@dataclass(frozen=True)
class TVRuleSpec:
    schema_version: str
    rule_id: str
    title: str
    timeframe: str
    source_sha256: str
    source_license: str
    provenance: str
    assignments: tuple[TVAssignmentSpec, ...]
    long_expression: str | None
    short_expression: str | None
    exit: TVExitSpec
    supported_functions: tuple[str, ...]
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    safe_for_local_replay: bool
    can_trade: bool = False
    can_promote: bool = False


@dataclass(frozen=True)
class TVRuleEvaluation:
    rows: int
    start: str | None
    end: str | None
    warmup_rows: int
    long_signals: int
    short_signals: int
    deterministic_hash: str
    can_trade: bool = False
    can_promote: bool = False


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<number>(?:\d+(?:\.\d*)?|\.\d+))|"
    r"(?P<string>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')|"
    r"(?P<identifier>[A-Za-z_][A-Za-z0-9_.]*)|"
    r"(?P<operator>>=|<=|==|!=|[+\-*/%><(),\[\]])|"
    r"(?P<invalid>.)"
    r")"
)


class RuleExpressionError(ValueError):
    """Raised when a Pine expression is outside the supported safe subset."""


class _ExpressionParser:
    _PRECEDENCE = {
        "or": 1,
        "and": 2,
        "==": 3,
        "!=": 3,
        ">": 3,
        ">=": 3,
        "<": 3,
        "<=": 3,
        "+": 4,
        "-": 4,
        "*": 5,
        "/": 5,
        "%": 5,
    }

    def __init__(self, expression: str):
        self.tokens = _tokenize(expression)
        self.position = 0

    def parse(self):
        if not self.tokens:
            raise RuleExpressionError("empty expression")
        node = self._parse_binary(1)
        if self.position != len(self.tokens):
            token = self.tokens[self.position]
            raise RuleExpressionError(f"unexpected token {token.value!r}")
        return node

    def _peek(self) -> _Token | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self) -> _Token:
        token = self._peek()
        if token is None:
            raise RuleExpressionError("unexpected end of expression")
        self.position += 1
        return token

    def _parse_binary(self, minimum_precedence: int):
        left = self._parse_unary()
        while True:
            token = self._peek()
            operator = token.value.lower() if token is not None else ""
            precedence = self._PRECEDENCE.get(operator, 0)
            if precedence < minimum_precedence:
                return left
            self._take()
            right = self._parse_binary(precedence + 1)
            left = ("binary", operator, left, right)

    def _parse_unary(self):
        token = self._peek()
        if token is not None and token.value.lower() in {"not", "+", "-"}:
            operator = self._take().value.lower()
            return ("unary", operator, self._parse_unary())
        return self._parse_primary()

    def _parse_primary(self):
        token = self._take()
        if token.value == "(":
            node = self._parse_binary(1)
            self._expect(")")
            return node
        if token.kind == "number":
            return ("literal", float(token.value))
        if token.kind == "string":
            return ("literal", bytes(token.value[1:-1], "utf-8").decode("unicode_escape"))
        if token.kind != "identifier":
            raise RuleExpressionError(f"unexpected token {token.value!r}")

        lowered = token.value.lower()
        if lowered in {"true", "false"}:
            return ("literal", lowered == "true")
        node = ("identifier", token.value)
        if self._peek() is not None and self._peek().value == "(":
            self._take()
            args = []
            if self._peek() is not None and self._peek().value != ")":
                while True:
                    args.append(self._parse_binary(1))
                    if self._peek() is None or self._peek().value != ",":
                        break
                    self._take()
            self._expect(")")
            node = ("call", token.value, tuple(args))
        while self._peek() is not None and self._peek().value == "[":
            self._take()
            offset = self._take()
            if offset.kind != "number" or not float(offset.value).is_integer():
                raise RuleExpressionError("history offsets must be non-negative integers")
            self._expect("]")
            node = ("history", node, int(float(offset.value)))
        return node

    def _expect(self, value: str) -> None:
        token = self._take()
        if token.value != value:
            raise RuleExpressionError(f"expected {value!r}, found {token.value!r}")


def _tokenize(expression: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    position = 0
    while position < len(expression):
        match = _TOKEN_RE.match(expression, position)
        if match is None:
            raise RuleExpressionError(f"cannot tokenize expression near {expression[position:]!r}")
        position = match.end()
        kind = match.lastgroup or "invalid"
        value = match.group(kind)
        if kind == "invalid":
            raise RuleExpressionError(f"unsupported character {value!r}")
        tokens.append(_Token(kind=kind, value=value))
    return tuple(tokens)


def _strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(line.split("//", 1)[0] for line in source.splitlines())


def _first_argument(expression: str) -> str:
    start = expression.find("(")
    if start < 0:
        return expression
    depth = 0
    quote: str | None = None
    for index, char in enumerate(expression[start + 1 :], start=start + 1):
        if quote:
            if char == quote and expression[index - 1] != "\\":
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return expression[start + 1 : index].strip()
            depth -= 1
        elif char == "," and depth == 0:
            return expression[start + 1 : index].strip()
    return expression[start + 1 :].strip()


def _split_call_arguments(text: str) -> tuple[str, ...]:
    args: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            current.append(char)
            if char == quote and (index == 0 or text[index - 1] != "\\"):
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
        elif char in "([":
            depth += 1
            current.append(char)
        elif char in ")]":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        args.append("".join(current).strip())
    return tuple(args)


def _call_body(line: str, function: str) -> str | None:
    marker = f"{function}("
    start = line.find(marker)
    if start < 0:
        return None
    start += len(marker)
    depth = 0
    quote: str | None = None
    for index in range(start, len(line)):
        char = line[index]
        if quote:
            if char == quote and line[index - 1] != "\\":
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return line[start:index]
            depth -= 1
    return None


def _named_argument(args: Sequence[str], name: str) -> str | None:
    prefix = f"{name}="
    for arg in args:
        compact = re.sub(r"\s+", "", arg)
        if compact.startswith(prefix):
            return arg.split("=", 1)[1].strip()
    return None


def _condition_from_alert(line: str) -> tuple[str, str] | None:
    body = _call_body(line, "alertcondition")
    if body is None:
        return None
    args = _split_call_arguments(body)
    if not args:
        return None
    description = " ".join(args[1:]).lower()
    if re.search(r"\b(long|buy|bull)\b", description):
        return "long", args[0]
    if re.search(r"\b(short|sell|bear)\b", description):
        return "short", args[0]
    return None


def _combine(expressions: Iterable[str]) -> str | None:
    unique = tuple(dict.fromkeys(expr.strip() for expr in expressions if expr.strip()))
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    return " or ".join(f"({expression})" for expression in unique)


def compile_pine_rule_spec(
    source: str,
    *,
    title: str,
    timeframe: str,
    source_license: str,
    provenance: str = "user_supplied",
    rule_id: str | None = None,
) -> TVRuleSpec:
    """Compile a conservative Pine subset into an immutable research spec.

    Compilation is fail-closed: source/provenance, repaint constructs,
    unsupported functions, and unparsable expressions become blockers.
    """

    if not source.strip():
        raise ValueError("Pine source cannot be empty")
    if not title.strip():
        raise ValueError("title cannot be empty")
    if not re.fullmatch(r"(?:[1-9][0-9]*[mhdwM]|[1-9][0-9]*)", timeframe):
        raise ValueError("timeframe must look like 1m, 5m, 1h, 4h, 1D, or Pine minutes")

    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    normalized_id = rule_id or re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    normalized_id = normalized_id or f"pine_{digest[:12]}"
    clean = _strip_comments(source)
    warnings: list[str] = []
    blockers: list[str] = []

    license_allowed = source_license in ALLOWED_LICENSES or provenance == "user_supplied"
    if not license_allowed:
        blockers.append("source_license_not_approved")
    if provenance not in {"user_supplied", "public_open_source", "local_owned"}:
        blockers.append("source_provenance_not_approved")

    for label, pattern in REPAINT_BLOCKERS.items():
        if re.search(pattern, clean, flags=re.IGNORECASE | re.MULTILINE):
            blockers.append(label)
    for label, pattern in UNSUPPORTED_PATTERNS.items():
        if re.search(pattern, clean, flags=re.IGNORECASE | re.MULTILINE):
            blockers.append(label)

    used_functions = sorted(set(re.findall(r"\b(?:ta|math|input)\.[A-Za-z_]\w*", clean)))
    unsupported_functions = sorted(set(used_functions) - SUPPORTED_FUNCTIONS)
    blockers.extend(f"unsupported_function:{name}" for name in unsupported_functions)

    assignment_re = re.compile(
        r"^\s*(?:(?:var|float|int|bool|string|color)\s+)*"
        r"([A-Za-z_]\w*)\s*=\s*(.+?)\s*$"
    )
    assignments: list[TVAssignmentSpec] = []
    assignment_names: set[str] = set()
    long_expressions: list[str] = []
    short_expressions: list[str] = []
    current_if: tuple[int, str] | None = None
    exit_spec = TVExitSpec()

    lines = clean.splitlines()
    for line in lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if stripped.startswith("if "):
            current_if = (indent, stripped[3:].strip())
            continue
        if current_if is not None and indent <= current_if[0]:
            current_if = None

        assignment_match = assignment_re.match(line)
        if assignment_match and not stripped.startswith(("strategy.", "alertcondition", "plot")):
            name, expression = assignment_match.groups()
            if name in assignment_names:
                blockers.append(f"duplicate_assignment:{name}")
                continue
            assignment_names.add(name)
            if expression.startswith("input."):
                role: Literal["input", "indicator", "derived"] = "input"
                expression = _first_argument(expression)
            elif re.search(r"\bta\.[A-Za-z_]\w*\s*\(", expression):
                role = "indicator"
            else:
                role = "derived"
            assignments.append(TVAssignmentSpec(name=name, expression=expression, role=role))
            continue

        entry_body = _call_body(stripped, "strategy.entry")
        if entry_body is not None:
            args = _split_call_arguments(entry_body)
            side = "long" if "strategy.long" in entry_body else "short" if "strategy.short" in entry_body else ""
            condition = _named_argument(args, "when") or (current_if[1] if current_if else "")
            if not condition:
                warnings.append("unconditional_strategy_entry_ignored")
            elif side == "long":
                long_expressions.append(condition)
            elif side == "short":
                short_expressions.append(condition)
            else:
                blockers.append("strategy_entry_side_unknown")
            continue

        alert = _condition_from_alert(stripped)
        if alert is not None:
            side, condition = alert
            (long_expressions if side == "long" else short_expressions).append(condition)
            continue

        exit_body = _call_body(stripped, "strategy.exit")
        if exit_body is not None:
            args = _split_call_arguments(exit_body)
            exit_spec = TVExitSpec(
                stop_expression=_named_argument(args, "stop"),
                limit_expression=_named_argument(args, "limit"),
                loss_expression=_named_argument(args, "loss"),
                profit_expression=_named_argument(args, "profit"),
                trail_expression=(
                    _named_argument(args, "trail_price")
                    or _named_argument(args, "trail_points")
                    or _named_argument(args, "trail_offset")
                ),
            )

    # Indicators often expose conditions through plotshape rather than a
    # strategy entry.  Prefer clearly named assignments and keep ambiguity out.
    if not long_expressions:
        long_expressions.extend(
            item.name
            for item in assignments
            if re.search(r"(?:^|_)(?:long|buy|bull)(?:_|$)", item.name, re.IGNORECASE)
        )
    if not short_expressions:
        short_expressions.extend(
            item.name
            for item in assignments
            if re.search(r"(?:^|_)(?:short|sell|bear)(?:_|$)", item.name, re.IGNORECASE)
        )

    long_expression = _combine(long_expressions)
    short_expression = _combine(short_expressions)
    if long_expression is None and short_expression is None:
        blockers.append("no_directional_signal_contract")

    expressions = [item.expression for item in assignments]
    expressions.extend(expr for expr in (long_expression, short_expression) if expr)
    expressions.extend(
        expr
        for expr in asdict(exit_spec).values()
        if isinstance(expr, str) and expr.strip()
    )
    for expression in expressions:
        try:
            _ExpressionParser(expression).parse()
        except RuleExpressionError as exc:
            blockers.append(f"unsupported_expression:{expression}:{exc}")

    blockers = list(dict.fromkeys(blockers))
    warnings = list(dict.fromkeys(warnings))
    return TVRuleSpec(
        schema_version="vnedge.tv_rule_spec.v1",
        rule_id=normalized_id,
        title=title.strip(),
        timeframe=timeframe,
        source_sha256=digest,
        source_license=source_license,
        provenance=provenance,
        assignments=tuple(assignments),
        long_expression=long_expression,
        short_expression=short_expression,
        exit=exit_spec,
        supported_functions=tuple(used_functions),
        warnings=tuple(warnings),
        blockers=tuple(blockers),
        safe_for_local_replay=not blockers,
    )


def _as_series(value, index: pd.Index) -> pd.Series:
    if isinstance(value, pd.Series):
        return value.reindex(index)
    return pd.Series(value, index=index)


def _wilder_rsi(series: pd.Series, window: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    average_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    average_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    relative_strength = average_gain / average_loss.replace(0.0, np.nan)
    result = 100.0 - (100.0 / (1.0 + relative_strength))
    return result.where(average_loss.ne(0.0), 100.0)


def _positive_int(value, name: str) -> int:
    number = float(value)
    if not number.is_integer() or number <= 0:
        raise RuleExpressionError(f"{name} must be a positive integer")
    return int(number)


def _evaluate_node(node, environment: dict[str, object], frame: pd.DataFrame):
    kind = node[0]
    if kind == "literal":
        return node[1]
    if kind == "identifier":
        name = node[1]
        if name not in environment:
            raise RuleExpressionError(f"unknown identifier {name!r}")
        return environment[name]
    if kind == "history":
        value = _evaluate_node(node[1], environment, frame)
        if not isinstance(value, pd.Series):
            raise RuleExpressionError("history operator requires a series")
        return value.shift(node[2])
    if kind == "unary":
        operator, child = node[1], _evaluate_node(node[2], environment, frame)
        if operator == "not":
            return ~_as_series(child, frame.index).fillna(False).astype(bool)
        return +child if operator == "+" else -child
    if kind == "binary":
        operator = node[1]
        left = _evaluate_node(node[2], environment, frame)
        right = _evaluate_node(node[3], environment, frame)
        if operator == "and":
            return _as_series(left, frame.index).fillna(False).astype(bool) & _as_series(
                right, frame.index
            ).fillna(False).astype(bool)
        if operator == "or":
            return _as_series(left, frame.index).fillna(False).astype(bool) | _as_series(
                right, frame.index
            ).fillna(False).astype(bool)
        operations = {
            "+": lambda: left + right,
            "-": lambda: left - right,
            "*": lambda: left * right,
            "/": lambda: left / right,
            "%": lambda: left % right,
            ">": lambda: left > right,
            ">=": lambda: left >= right,
            "<": lambda: left < right,
            "<=": lambda: left <= right,
            "==": lambda: left == right,
            "!=": lambda: left != right,
        }
        return operations[operator]()
    if kind != "call":
        raise RuleExpressionError(f"unknown expression node {kind!r}")

    name = node[1]
    args = [_evaluate_node(arg, environment, frame) for arg in node[2]]
    if name not in SUPPORTED_FUNCTIONS:
        raise RuleExpressionError(f"unsupported function {name!r}")
    if name.startswith("input."):
        if not args:
            raise RuleExpressionError(f"{name} requires a default value")
        return args[0]
    if name == "math.abs":
        return abs(args[0])
    if name == "math.max":
        return np.maximum(args[0], args[1])
    if name == "math.min":
        return np.minimum(args[0], args[1])
    if name == "ta.sma":
        return _as_series(args[0], frame.index).rolling(
            _positive_int(args[1], name), min_periods=_positive_int(args[1], name)
        ).mean()
    if name == "ta.ema":
        window = _positive_int(args[1], name)
        return _as_series(args[0], frame.index).ewm(
            span=window, adjust=False, min_periods=window
        ).mean()
    if name == "ta.rsi":
        return _wilder_rsi(
            _as_series(args[0], frame.index), _positive_int(args[1], name)
        )
    if name == "ta.atr":
        window = _positive_int(args[0], name)
        previous_close = frame["close"].shift(1)
        true_range = pd.concat(
            [
                frame["high"] - frame["low"],
                (frame["high"] - previous_close).abs(),
                (frame["low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return true_range.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    if name in {"ta.highest", "ta.lowest"}:
        window = _positive_int(args[1], name)
        rolling = _as_series(args[0], frame.index).rolling(window, min_periods=window)
        return rolling.max() if name == "ta.highest" else rolling.min()
    if name == "ta.roc":
        return _as_series(args[0], frame.index).pct_change(
            _positive_int(args[1], name), fill_method=None
        ) * 100.0
    if name == "ta.stdev":
        window = _positive_int(args[1], name)
        return _as_series(args[0], frame.index).rolling(window, min_periods=window).std(ddof=0)
    if name in {"ta.crossover", "ta.crossunder"}:
        left = _as_series(args[0], frame.index)
        right = _as_series(args[1], frame.index)
        if name == "ta.crossover":
            return (left > right) & (left.shift(1) <= right.shift(1))
        return (left < right) & (left.shift(1) >= right.shift(1))
    raise RuleExpressionError(f"unsupported function {name!r}")


def evaluate_rule_spec(spec: TVRuleSpec, candles: pd.DataFrame) -> tuple[pd.DataFrame, TVRuleEvaluation]:
    """Evaluate one safe rule spec on immutable, chronological closed candles."""

    if not spec.safe_for_local_replay:
        raise ValueError(f"rule is blocked: {', '.join(spec.blockers)}")
    required = {"open", "high", "low", "close", "volume"}
    missing = sorted(required - set(candles.columns))
    if missing:
        raise ValueError(f"candles missing required columns: {', '.join(missing)}")
    if candles.empty:
        raise ValueError("candles cannot be empty")
    if not candles.index.is_monotonic_increasing or candles.index.has_duplicates:
        raise ValueError("candles must have a unique, chronological index")

    frame = candles.loc[:, sorted(required)].copy()
    environment: dict[str, object] = {
        name: frame[name].astype(float) for name in required
    }
    output = pd.DataFrame(index=frame.index)
    for assignment in spec.assignments:
        node = _ExpressionParser(assignment.expression).parse()
        value = _evaluate_node(node, environment, frame)
        environment[assignment.name] = value
        output[assignment.name] = _as_series(value, frame.index)

    for side, expression in (
        ("long_signal", spec.long_expression),
        ("short_signal", spec.short_expression),
    ):
        if expression is None:
            output[side] = False
        else:
            value = _evaluate_node(_ExpressionParser(expression).parse(), environment, frame)
            output[side] = _as_series(value, frame.index).fillna(False).astype(bool)

    ready = output[["long_signal", "short_signal"]].notna().all(axis=1)
    indicator_columns = [item.name for item in spec.assignments if item.role == "indicator"]
    if indicator_columns:
        ready &= output[indicator_columns].notna().all(axis=1)
    warmup_rows = int((~ready).cumprod().sum())
    signature_rows = [
        f"{index}|{int(long_signal)}|{int(short_signal)}"
        for index, long_signal, short_signal in zip(
            output.index.astype(str), output["long_signal"], output["short_signal"]
        )
    ]
    deterministic_hash = hashlib.sha256("\n".join(signature_rows).encode("utf-8")).hexdigest()
    evaluation = TVRuleEvaluation(
        rows=len(output),
        start=str(output.index[0]) if len(output) else None,
        end=str(output.index[-1]) if len(output) else None,
        warmup_rows=warmup_rows,
        long_signals=int(output["long_signal"].sum()),
        short_signals=int(output["short_signal"].sum()),
        deterministic_hash=deterministic_hash,
    )
    return output, evaluation


def build_rule_artifact(
    spec: TVRuleSpec,
    *,
    evaluation: TVRuleEvaluation | None = None,
) -> dict[str, object]:
    return {
        "adapter_id": TV_RULE_ADAPTER_ID,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "READY_FOR_LOCAL_REPLAY" if spec.safe_for_local_replay else "QUARANTINED",
        "summary": {
            "assignments": len(spec.assignments),
            "indicator_assignments": sum(item.role == "indicator" for item in spec.assignments),
            "long_contract": spec.long_expression is not None,
            "short_contract": spec.short_expression is not None,
            "blockers": len(spec.blockers),
            "warnings": len(spec.warnings),
            "evaluated": evaluation is not None,
        },
        "rule_spec": asdict(spec),
        "evaluation": asdict(evaluation) if evaluation is not None else None,
        "policy": {
            "network_access": False,
            "tradingview_data_used": False,
            "unofficial_tvscreener_dependency": False,
            "local_closed_candles_only": True,
            "raw_source_emitted": False,
            "normalized_rule_expressions_emitted": True,
            "unsupported_constructs_fail_closed": True,
            "research_only": True,
        },
        "operator_answer": (
            "Rule contract is locally replayable on Delta candles; it is not registered in the "
            "strategy or execution path."
            if spec.safe_for_local_replay
            else "Source is quarantined until every listed blocker is removed or manually ported."
        ),
        "can_trade": False,
        "can_promote": False,
    }


def publish_rule_artifact(payload: dict[str, object], out: Path | str = DEFAULT_OUT) -> Path:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _load_candles(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
    else:
        frame = pd.read_csv(path)
    timestamp_column = next(
        (name for name in ("timestamp", "ts", "datetime", "date") if name in frame.columns),
        None,
    )
    if timestamp_column is not None:
        frame[timestamp_column] = pd.to_datetime(frame[timestamp_column], utc=True)
        frame = frame.set_index(timestamp_column)
    return frame


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile lawful local Pine source into a VNEDGE research RuleSpec."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--license", dest="source_license", default="user-supplied")
    parser.add_argument(
        "--provenance",
        choices=("user_supplied", "public_open_source", "local_owned"),
        default="user_supplied",
    )
    parser.add_argument("--rule-id")
    parser.add_argument("--candles", type=Path)
    parser.add_argument("--signals-out", type=Path)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    source = args.source.read_text(encoding="utf-8")
    spec = compile_pine_rule_spec(
        source,
        title=args.title,
        timeframe=args.timeframe,
        source_license=args.source_license,
        provenance=args.provenance,
        rule_id=args.rule_id,
    )
    evaluation = None
    if args.candles is not None:
        signals, evaluation = evaluate_rule_spec(spec, _load_candles(args.candles))
        if args.signals_out is not None:
            args.signals_out.parent.mkdir(parents=True, exist_ok=True)
            if args.signals_out.suffix.lower() == ".parquet":
                signals.to_parquet(args.signals_out)
            else:
                signals.to_csv(args.signals_out)
    path = publish_rule_artifact(build_rule_artifact(spec, evaluation=evaluation), args.out)
    print(path)
    return 0 if spec.safe_for_local_replay else 2


if __name__ == "__main__":
    raise SystemExit(main())
