"""Plugin manifest contract for AI-authored strategies.

The AI sandbox (:mod:`vnedge.strategy.ai_sandbox`) already proves a strategy is
*safe to load* (deny-by-default AST + restricted exec). This adds the missing
*declaration* layer, adapted from OpenTerminalUI's plugin contract: each plugin
ships a ``plugin.yaml`` declaring what it is and — critically — its
``required_permissions``.

The whole point is the permission whitelist. Permissions are drawn ONLY from
:data:`GRANTABLE_PERMISSIONS` — a set that contains nothing capable of placing
an order, reaching the network, or touching the filesystem. Anything in
:data:`FORBIDDEN_PERMISSIONS` (``trade``, ``network``, ...) fails the manifest
loudly. So a plugin manifest can *never* grant trade access: this is the
manifest-layer echo of the gateway invariant — AI code is research-only,
``can_trade`` is structurally False. It is a third independent guard alongside
the sandbox and the pre-trade gateway; it does not replace either.

A plugin's lifecycle hook is its ``BaseStrategy.signal`` — invoked once per
CLOSED bar (an ``on_bar_close`` hook by another name), consistent with the whole
codebase's "decisions at close" discipline. The manifest validates the loaded
class actually implements it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from vnedge.strategy.ai_sandbox import SandboxViolation, load_ai_strategy
from vnedge.strategy.base_strategy import BaseStrategy

MANIFEST_FILENAME = "plugin.yaml"

#: Permissions an AI plugin MAY request. None of these can place an order, reach
#: the network, or touch the filesystem — so the manifest can never grant trade.
GRANTABLE_PERMISSIONS: frozenset[str] = frozenset(
    {"read_candles", "read_indicators", "read_funding", "emit_signal", "compute"}
)

#: Permissions that are NEVER grantable. Requesting any of these fails the
#: manifest — the structural enforcement of can_trade=False.
FORBIDDEN_PERMISSIONS: frozenset[str] = frozenset(
    {
        "trade",
        "place_order",
        "cancel_order",
        "network",
        "filesystem",
        "read_secrets",
        "modify_config",
        "promote",
        "kill_switch",
    }
)

#: The one lifecycle hook a plugin must implement — BaseStrategy.signal runs
#: once per closed bar (== on_bar_close).
LIFECYCLE_HOOK = "on_bar_close"


class PluginManifestError(ValueError):
    """Raised when a manifest is malformed or requests a forbidden permission.
    Carries every problem, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class PluginManifest:
    name: str
    entry: str  # path to the strategy .py, relative to the manifest
    required_permissions: frozenset[str]
    version: str = "0"
    author: str = ""
    description: str = ""

    @property
    def can_trade(self) -> bool:
        # Structurally, always. A manifest cannot grant trade access; the
        # gateway is still the runtime authority regardless.
        return False

    def problems(self) -> list[str]:
        out: list[str] = []
        if not self.name.strip():
            out.append("manifest 'name' is required")
        if not self.entry.strip():
            out.append("manifest 'entry' (strategy .py) is required")
        if ".." in Path(self.entry).parts or Path(self.entry).is_absolute():
            out.append(f"entry {self.entry!r} must be a relative path inside the plugin dir")
        forbidden = self.required_permissions & FORBIDDEN_PERMISSIONS
        if forbidden:
            out.append(
                f"required_permissions requests forbidden permission(s) "
                f"{sorted(forbidden)} — a plugin can never be granted trade/network/fs access"
            )
        unknown = self.required_permissions - GRANTABLE_PERMISSIONS - FORBIDDEN_PERMISSIONS
        if unknown:
            out.append(
                f"required_permissions has unknown permission(s) {sorted(unknown)} "
                f"(grantable: {sorted(GRANTABLE_PERMISSIONS)})"
            )
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entry": self.entry,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "required_permissions": sorted(self.required_permissions),
            "can_trade": self.can_trade,
        }


def parse_manifest(data: dict[str, Any]) -> PluginManifest:
    if not isinstance(data, dict):
        raise PluginManifestError(["plugin.yaml must be a mapping"])
    perms = data.get("required_permissions", []) or []
    if not isinstance(perms, (list, tuple, set)):
        raise PluginManifestError(["'required_permissions' must be a list"])
    manifest = PluginManifest(
        name=str(data.get("name", "")),
        entry=str(data.get("entry", "")),
        required_permissions=frozenset(str(p) for p in perms),
        version=str(data.get("version", "0")),
        author=str(data.get("author", "")),
        description=str(data.get("description", "")),
    )
    problems = manifest.problems()
    if problems:
        raise PluginManifestError(problems)
    return manifest


def load_manifest(path: str | Path) -> PluginManifest:
    text = Path(path).read_text(encoding="utf-8")
    return parse_manifest(yaml.safe_load(text) or {})


@dataclass(frozen=True)
class LoadedPlugin:
    manifest: PluginManifest
    strategy_cls: type[BaseStrategy]
    granted_permissions: frozenset[str] = field(default_factory=frozenset)


def load_plugin(plugin_dir: str | Path) -> LoadedPlugin:
    """Load one plugin dir: validate the manifest (deny-by-default permissions),
    then load its entry through the AI sandbox. Raises PluginManifestError or
    SandboxViolation — never partially loads."""
    directory = Path(plugin_dir)
    manifest = load_manifest(directory / MANIFEST_FILENAME)
    entry = directory / manifest.entry
    if not entry.is_file():
        raise PluginManifestError([f"entry {manifest.entry!r} not found in {directory}"])
    strategy_cls = load_ai_strategy(entry.read_text(encoding="utf-8"), module_name=f"plugin_{manifest.name}")
    if not hasattr(strategy_cls, "signal"):
        raise PluginManifestError(
            [f"plugin {manifest.name!r} strategy has no signal() — the {LIFECYCLE_HOOK} hook"]
        )
    # Granted == requested, because requested is already whitelist-constrained.
    return LoadedPlugin(
        manifest=manifest,
        strategy_cls=strategy_cls,
        granted_permissions=manifest.required_permissions,
    )


def load_plugins_from_dir(root: str | Path = "data/strategies/ai") -> dict[str, LoadedPlugin]:
    """Load every immediate subdir containing a ``plugin.yaml``. A bad plugin is
    skipped (with its problems attached), never crashing the loader."""
    import logging

    logger = logging.getLogger(__name__)
    base = Path(root)
    out: dict[str, LoadedPlugin] = {}
    if not base.is_dir():
        return out
    for sub in sorted(p for p in base.iterdir() if p.is_dir()):
        if not (sub / MANIFEST_FILENAME).is_file():
            continue
        try:
            plugin = load_plugin(sub)
        except (PluginManifestError, SandboxViolation) as exc:
            logger.warning("plugin %s rejected: %s", sub.name, exc)
            continue
        out[plugin.manifest.name] = plugin
    return out
