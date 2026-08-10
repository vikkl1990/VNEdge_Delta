"""AI strategy sandbox — load AI-authored strategy code safely.

The problem this solves: we want the *generation* leverage of "vibe trading"
(let an AI write a strategy) without ever letting AI-authored code touch
execution, the network, the filesystem, or the core sources. An AI strategy
is exactly a research candidate: it must clear the same walk-forward gates,
the same causality analyzer, the same pre-registered untouched-data judgment,
and the same human approval as every hand-written strategy. Nothing here
trades, promotes, or auto-registers.

Two layers of defense, in order:

1. ``validate_strategy_source`` — a **deny-by-default** AST validator. The
   source is parsed and every node is checked against an allowlist of node
   types plus a set of explicit prohibitions (imports outside a tiny
   whitelist, ``eval``/``exec``/``open``/``__import__``, dunder attribute
   access like ``__globals__``/``__builtins__``/``__class__``, ``os``/``sys``/
   ``subprocess``/``socket``/``pathlib`` usage, and lookahead patterns such as
   ``shift(-1)`` or ``iloc[index + 1]``). Anything the validator does not
   positively recognise is rejected.

2. ``load_ai_strategy`` — validate first (raising :class:`SandboxViolation`
   on any violation), then ``exec`` the source in a **restricted namespace**:
   the builtins are a small safe subset with no ``open``/``eval``/``exec``/
   ``compile``, and ``__import__`` is replaced by a guard that only admits the
   whitelisted modules. Even if a pattern somehow slipped past the AST check,
   the runtime import guard blocks it. The loaded class's ``strategy_id`` is
   force-prefixed ``ai_`` so an AI strategy is always distinguishable.

``ai_strategies_from_dir`` loads a whole directory, skipping (with a logged
reason) any file that fails validation — a malformed AI strategy can never
crash the loader or the research loop.
"""

from __future__ import annotations

import ast
import importlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from vnedge.strategy.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)

# --- Whitelists / blocklists ---------------------------------------------------

#: Namespace every AI-authored strategy_id is force-prefixed with, so an AI
#: strategy is always distinguishable from a hand-written / registered one.
AI_STRATEGY_ID_PREFIX = "ai_"

#: Top-level packages an AI strategy may import freely (submodules allowed).
ALLOWED_IMPORT_ROOTS = frozenset({"pandas", "numpy", "math", "dataclasses"})

#: Fully-qualified modules allowed by exact match. ``vnedge`` is deliberately
#: NOT a permitted root — only these two curated modules are importable, so an
#: AI strategy can never reach data/network/execution code under ``vnedge``.
ALLOWED_IMPORT_EXACT = frozenset(
    {
        "vnedge.strategy.indicators",
        "vnedge.strategy.base_strategy",
        "__future__",  # compiler directive (e.g. `from __future__ import annotations`)
    }
)

#: Builtin/callable names that must never be *called* from AI source. Broader
#: than the strict minimum on purpose (deny by default): ``getattr`` &co. are a
#: classic sandbox-escape primitive even though the AST also blocks dunder
#: attribute access.
FORBIDDEN_CALL_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "__build_class__",
        "open",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "globals",
        "locals",
        "input",
        "breakpoint",
        "exit",
        "quit",
        "help",
        "memoryview",
    }
)

#: Module/identifier names whose mere appearance (as a bare ``Name``) is a
#: rejection: filesystem, process, network, reflection, serialisation.
FORBIDDEN_NAMES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "pathlib",
        "shutil",
        "importlib",
        "imp",
        "ctypes",
        "threading",
        "multiprocessing",
        "asyncio",
        "requests",
        "urllib",
        "http",
        "httpx",
        "aiohttp",
        "builtins",
        "gc",
        "inspect",
        "pickle",
        "marshal",
        "code",
        "codeop",
        "pty",
        "platform",
        "resource",
        "signal",
        "fcntl",
        "mmap",
        "tempfile",
        "glob",
        "io",
        "sqlite3",
        "psutil",
        "ftplib",
        "smtplib",
        "webbrowser",
        "site",
        "runpy",
        "pkgutil",
        "types",
        "__builtins__",
    }
)

#: AST node type NAMES that are permitted. Deny by default: any node whose
#: type name is not in this set is a violation ("disallowed_syntax"). The set
#: covers everything a causal, self-contained strategy needs (arithmetic,
#: comparisons, comprehensions, control flow, class/def) and deliberately
#: excludes escapes: ``Global``/``Nonlocal``/``Delete``/``With``/``Try``/
#: ``async``/``await``/``yield``/walrus.
ALLOWED_NODE_NAMES = frozenset(
    {
        "Module",
        "Import",
        "ImportFrom",
        "alias",
        "ClassDef",
        "FunctionDef",
        "arguments",
        "arg",
        "Assign",
        "AnnAssign",
        "AugAssign",
        "Return",
        "Pass",
        "Break",
        "Continue",
        "Raise",
        "Assert",
        "If",
        "For",
        "While",
        "IfExp",
        "Expr",
        "Call",
        "keyword",
        "Attribute",
        "Name",
        "Subscript",
        "Slice",
        "Load",
        "Store",
        "Constant",
        "BinOp",
        "UnaryOp",
        "BoolOp",
        "Compare",
        "Tuple",
        "List",
        "Dict",
        "Set",
        "ListComp",
        "SetComp",
        "DictComp",
        "GeneratorExp",
        "comprehension",
        "Lambda",
        "Starred",
        "JoinedStr",
        "FormattedValue",
        # binary / unary / bool / comparison operators
        "Add",
        "Sub",
        "Mult",
        "Div",
        "FloorDiv",
        "Mod",
        "Pow",
        "MatMult",
        "LShift",
        "RShift",
        "BitOr",
        "BitAnd",
        "BitXor",
        "UAdd",
        "USub",
        "Not",
        "Invert",
        "And",
        "Or",
        "Eq",
        "NotEq",
        "Lt",
        "LtE",
        "Gt",
        "GtE",
        "Is",
        "IsNot",
        "In",
        "NotIn",
    }
)

_DUNDER = re.compile(r"^__.*__$")
_POSITIONAL_INDEXERS = frozenset({"iloc", "loc", "iat", "at"})


# --- Report types --------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    node_type: str
    lineno: int
    reason: str

    def describe(self) -> str:
        return f"line {self.lineno} [{self.node_type}]: {self.reason}"


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    violations: tuple[Violation, ...]
    strategy_class_name: str | None = None
    strategy_id: str | None = None

    def describe(self) -> str:
        head = "OK" if self.ok else f"REJECT ({len(self.violations)} violation(s))"
        lines = [head]
        lines.extend("  " + v.describe() for v in self.violations)
        return "\n".join(lines)


class SandboxViolation(Exception):
    """Raised by :func:`load_ai_strategy` when validation fails."""

    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        super().__init__(report.describe())


# --- AST helpers ---------------------------------------------------------------


def _lineno(node: ast.AST) -> int:
    return int(getattr(node, "lineno", 0) or 0)


def _import_allowed(name: str) -> bool:
    if name in ALLOWED_IMPORT_EXACT:
        return True
    root = name.split(".")[0]
    if root == "vnedge":  # only the two exact vnedge modules above
        return False
    if root in FORBIDDEN_NAMES:
        return False
    return root in ALLOWED_IMPORT_ROOTS


def _is_negative_const(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return isinstance(node.operand, ast.Constant) and isinstance(
            node.operand.value, (int, float)
        )
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and node.value < 0


def _positive_int_const(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
        and node.value > 0
    )


def _is_forward_offset(node: ast.AST) -> bool:
    """True for ``<name> + <positive int>`` / ``<positive int> + <name>`` (a
    future-row reference) or ``<name> - <negative const>``."""
    if not isinstance(node, ast.BinOp):
        return False
    if isinstance(node.op, ast.Add):
        left_name = isinstance(node.left, ast.Name)
        right_name = isinstance(node.right, ast.Name)
        if (left_name and _positive_int_const(node.right)) or (
            right_name and _positive_int_const(node.left)
        ):
            return True
    if isinstance(node.op, ast.Sub) and isinstance(node.left, ast.Name):
        if _is_negative_const(node.right):
            return True
    return False


def _shift_is_lookahead(call: ast.Call) -> bool:
    """``.shift(-n)`` / ``.shift(periods=-n)`` reads future rows into the past."""
    for arg in call.args[:1]:
        if _is_negative_const(arg):
            return True
    for kw in call.keywords:
        if kw.arg in {"periods", None} and _is_negative_const(kw.value):
            return True
    return False


def _subscript_index_nodes(sub: ast.Subscript) -> list[ast.AST]:
    """The index expression(s) of a subscript, unwrapping simple slices."""
    idx = sub.slice
    if isinstance(idx, ast.Slice):
        return [b for b in (idx.lower, idx.upper, idx.step) if b is not None]
    return [idx]


# --- Validation ----------------------------------------------------------------


def _is_base_strategy_base(base: ast.AST) -> bool:
    if isinstance(base, ast.Name):
        return base.id == "BaseStrategy"
    if isinstance(base, ast.Attribute):
        return base.attr == "BaseStrategy"
    return False


def _class_strategy_id(cls: ast.ClassDef) -> str | None:
    for stmt in cls.body:
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
            value = stmt.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "strategy_id":
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    return value.value
    return None


def _class_method_names(cls: ast.ClassDef) -> set[str]:
    return {stmt.name for stmt in cls.body if isinstance(stmt, ast.FunctionDef)}


def validate_strategy_source(source: str) -> ValidationReport:
    """Deny-by-default AST validation of AI-authored strategy source.

    Returns a :class:`ValidationReport`; ``ok`` is True only when there are
    zero violations AND exactly one well-formed ``BaseStrategy`` subclass.
    Never executes the source.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return ValidationReport(
            ok=False,
            violations=(
                Violation("SyntaxError", int(exc.lineno or 0), f"could not parse: {exc.msg}"),
            ),
        )

    violations: list[Violation] = []

    def add(node: ast.AST, reason: str) -> None:
        violations.append(Violation(type(node).__name__, _lineno(node), reason))

    for node in ast.walk(tree):
        name = type(node).__name__

        # (1) deny-by-default node-type allowlist
        if name not in ALLOWED_NODE_NAMES:
            add(node, f"disallowed syntax construct '{name}' (deny by default)")
            continue

        # (2) imports must be on the whitelist
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _import_allowed(alias.name):
                    add(node, f"import of non-whitelisted module '{alias.name}'")
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                add(node, "relative imports are not allowed")
            module = node.module or ""
            if not _import_allowed(module):
                add(node, f"import from non-whitelisted module '{module}'")

        # (3) forbidden bare names (os, sys, subprocess, dunders, ...)
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                add(node, f"forbidden name '{node.id}'")
            elif _DUNDER.match(node.id):
                add(node, f"dunder name access '{node.id}' is not allowed")

        # (4) forbidden attribute access: any dunder, plus forbidden module attrs
        elif isinstance(node, ast.Attribute):
            if _DUNDER.match(node.attr):
                add(node, f"dunder attribute access '.{node.attr}' is not allowed")
            if isinstance(node.value, ast.Name) and node.value.id in FORBIDDEN_NAMES:
                add(node, f"attribute access on forbidden module '{node.value.id}'")

        # (5) forbidden calls + lookahead call patterns
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALL_NAMES:
                add(node, f"call to forbidden builtin '{func.id}'")
            if isinstance(func, ast.Attribute):
                if func.attr in FORBIDDEN_CALL_NAMES:
                    add(node, f"call to forbidden method '.{func.attr}'")
                if func.attr == "shift" and _shift_is_lookahead(node):
                    add(node, "shift() with a negative period reads future rows (lookahead)")

        # (6) lookahead via positional indexers: iloc/loc[index + k]
        elif isinstance(node, ast.Subscript):
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr in _POSITIONAL_INDEXERS:
                for idx in _subscript_index_nodes(node):
                    if _is_forward_offset(idx):
                        add(
                            node,
                            f".{value.attr}[...] indexes a future row "
                            "(index + k / index - (-k)) — lookahead",
                        )

    # (7) structural requirement: exactly one BaseStrategy subclass with
    #     prepare + signal + a string strategy_id.
    strategy_classes = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and any(_is_base_strategy_base(b) for b in n.bases)
    ]
    class_name: str | None = None
    strategy_id: str | None = None
    if len(strategy_classes) == 0:
        violations.append(
            Violation("Module", 0, "no BaseStrategy subclass found — nothing to load")
        )
    elif len(strategy_classes) > 1:
        violations.append(
            Violation(
                "Module",
                _lineno(strategy_classes[1]),
                f"expected exactly one BaseStrategy subclass, found {len(strategy_classes)}",
            )
        )
    else:
        cls = strategy_classes[0]
        class_name = cls.name
        methods = _class_method_names(cls)
        for required in ("prepare", "signal"):
            if required not in methods:
                add(cls, f"BaseStrategy subclass '{cls.name}' is missing {required}()")
        strategy_id = _class_strategy_id(cls)
        if strategy_id is None:
            add(cls, f"BaseStrategy subclass '{cls.name}' must set a string strategy_id")

    return ValidationReport(
        ok=not violations,
        violations=tuple(violations),
        strategy_class_name=class_name,
        strategy_id=strategy_id,
    )


# --- Restricted execution ------------------------------------------------------


def _guarded_import(
    name: str,
    globals=None,  # noqa: A002 - shadowing matches the __import__ signature
    locals=None,  # noqa: A002
    fromlist=(),
    level: int = 0,
):
    """Replacement ``__import__`` that admits only whitelisted modules. This is
    defense-in-depth: validation already rejects bad imports, but a restricted
    exec namespace must not be able to import ``os`` even if something slipped
    through."""
    if level and level > 0:
        raise SandboxViolation(
            ValidationReport(
                ok=False,
                violations=(Violation("ImportFrom", 0, "relative import blocked at runtime"),),
            )
        )
    if not _import_allowed(name):
        raise SandboxViolation(
            ValidationReport(
                ok=False,
                violations=(Violation("Import", 0, f"runtime import of '{name}' blocked"),),
            )
        )
    return importlib.__import__(name, globals, locals, fromlist, level)


#: Builtins made available to AI source. No open/eval/exec/compile/__import__
#: (the last is overridden with the guard below); no getattr/setattr/globals.
_SAFE_BUILTIN_NAMES = (
    "abs",
    "min",
    "max",
    "sum",
    "round",
    "len",
    "range",
    "enumerate",
    "zip",
    "map",
    "filter",
    "sorted",
    "reversed",
    "float",
    "int",
    "str",
    "bool",
    "list",
    "dict",
    "tuple",
    "set",
    "frozenset",
    "isinstance",
    "issubclass",
    "hasattr",
    "print",
    "repr",
    "format",
    "divmod",
    "pow",
    "all",
    "any",
    "slice",
    "next",
    "iter",
    "super",
    "property",
    "staticmethod",
    "classmethod",
    "object",
    # __build_class__ is required for `class` statements to execute at all.
    "__build_class__",
    # exception types so strategies can raise/guard honestly
    "Exception",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
    "ZeroDivisionError",
    "ArithmeticError",
    "RuntimeError",
    "StopIteration",
    "NotImplementedError",
    "AttributeError",
    "OverflowError",
    "FloatingPointError",
)


def _safe_builtins() -> dict:
    import builtins as _b

    out: dict = {name: getattr(_b, name) for name in _SAFE_BUILTIN_NAMES if hasattr(_b, name)}
    out["__import__"] = _guarded_import
    return out


def _restricted_globals(module_name: str) -> dict:
    return {"__builtins__": _safe_builtins(), "__name__": module_name}


def load_ai_strategy(
    source: str,
    *,
    strategy_id_prefix: str = AI_STRATEGY_ID_PREFIX,
    module_name: str = "ai_strategy",
) -> type[BaseStrategy]:
    """Validate, then exec ``source`` in a restricted namespace and return the
    single ``BaseStrategy`` subclass it defines.

    Raises :class:`SandboxViolation` if validation fails. The returned class's
    ``strategy_id`` is force-prefixed ``ai_`` so AI strategies are always
    distinguishable from hand-written / registered strategies.
    """
    report = validate_strategy_source(source)
    if not report.ok:
        raise SandboxViolation(report)

    namespace = _restricted_globals(module_name)
    # Compile+exec the ALREADY-VALIDATED source only. `dont_inherit=True`
    # keeps our restricted __future__/flags from leaking the caller's.
    code = compile(source, f"<ai_strategy:{module_name}>", "exec", dont_inherit=True)
    exec(code, namespace)  # noqa: S102 - validated source, restricted builtins

    classes = [
        obj
        for obj in namespace.values()
        if isinstance(obj, type)
        and issubclass(obj, BaseStrategy)
        and obj is not BaseStrategy
    ]
    if len(classes) != 1:
        raise SandboxViolation(
            ValidationReport(
                ok=False,
                violations=(
                    Violation(
                        "Module",
                        0,
                        f"expected exactly one BaseStrategy subclass after exec, "
                        f"found {len(classes)}",
                    ),
                ),
            )
        )
    cls = classes[0]

    current = str(getattr(cls, "strategy_id", "") or "unnamed")
    if not current.startswith(strategy_id_prefix):
        cls.strategy_id = strategy_id_prefix + current
    return cls


def ai_strategies_from_dir(
    path: str | Path = "data/strategies/ai",
    *,
    strategy_id_prefix: str = AI_STRATEGY_ID_PREFIX,
) -> dict[str, type[BaseStrategy]]:
    """Load every ``*.py`` under ``path``. Files that fail validation (or raise
    on load) are skipped with a logged reason — a bad AI strategy can never
    crash the loader or the research loop. Keyed by (prefixed) ``strategy_id``.
    """
    directory = Path(path)
    loaded: dict[str, type[BaseStrategy]] = {}
    if not directory.is_dir():
        logger.info("AI sandbox dir %s does not exist — no AI strategies loaded", directory)
        return loaded

    for file in sorted(directory.glob("*.py")):
        if file.name.startswith("_"):  # __init__.py, private helpers
            continue
        try:
            source = file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("AI sandbox: could not read %s: %s", file.name, exc)
            continue
        try:
            cls = load_ai_strategy(source, module_name=f"ai_{file.stem}", strategy_id_prefix=strategy_id_prefix)
        except SandboxViolation as exc:
            logger.warning("AI sandbox: rejected %s — %s", file.name, exc.report.describe())
            continue
        except Exception as exc:  # noqa: BLE001 - a bad file must never crash the loader
            logger.warning("AI sandbox: failed to load %s: %s", file.name, exc)
            continue
        if cls.strategy_id in loaded:
            logger.warning(
                "AI sandbox: duplicate strategy_id '%s' from %s — keeping the first",
                cls.strategy_id,
                file.name,
            )
            continue
        loaded[cls.strategy_id] = cls
        logger.info("AI sandbox: loaded '%s' from %s", cls.strategy_id, file.name)
    return loaded
