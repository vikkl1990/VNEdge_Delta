"""Plugin manifest contract — the permission whitelist that structurally
enforces can_trade=False for AI-authored strategies."""

import pytest
import yaml

from vnedge.strategy.plugin_manifest import (
    LIFECYCLE_HOOK,
    PluginManifest,
    PluginManifestError,
    load_plugin,
    load_plugins_from_dir,
    parse_manifest,
)

# A minimal strategy that passes the AI sandbox (only whitelisted imports/nodes,
# has prepare + signal + a string strategy_id).
VALID_STRATEGY = '''
from vnedge.strategy.base_strategy import BaseStrategy


class MyPlugin(BaseStrategy):
    strategy_id = "my_plugin"

    def prepare(self, candles):
        return candles

    def signal(self, prepared, i):
        return None
'''


def _write_plugin(tmp_path, manifest: dict, strategy: str = VALID_STRATEGY, entry="strategy.py"):
    d = tmp_path / manifest.get("name", "plugin")
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.yaml").write_text(yaml.safe_dump(manifest))
    (d / entry).write_text(strategy)
    return d


# ---------------------------------------------------------------- manifest
def test_valid_manifest_parses_and_cannot_trade():
    m = parse_manifest({"name": "p", "entry": "s.py", "required_permissions": ["read_candles"]})
    assert m.name == "p" and "read_candles" in m.required_permissions
    assert m.can_trade is False


def test_requesting_trade_is_refused():
    with pytest.raises(PluginManifestError, match="forbidden permission"):
        parse_manifest({"name": "p", "entry": "s.py", "required_permissions": ["trade"]})
    with pytest.raises(PluginManifestError, match="forbidden permission"):
        parse_manifest({"name": "p", "entry": "s.py", "required_permissions": ["network"]})


def test_unknown_permission_is_refused():
    with pytest.raises(PluginManifestError, match="unknown permission"):
        parse_manifest({"name": "p", "entry": "s.py", "required_permissions": ["teleport"]})


def test_path_traversal_entry_is_refused():
    with pytest.raises(PluginManifestError, match="relative path"):
        parse_manifest({"name": "p", "entry": "../../etc/passwd", "required_permissions": []})


def test_can_trade_is_always_false_even_if_field_lies():
    m = PluginManifest(name="p", entry="s.py", required_permissions=frozenset())
    assert m.can_trade is False


# ------------------------------------------------------------------ loading
def test_load_valid_plugin(tmp_path):
    d = _write_plugin(tmp_path, {"name": "goodplug", "entry": "strategy.py",
                                 "required_permissions": ["read_candles", "emit_signal"]})
    plugin = load_plugin(d)
    assert plugin.manifest.name == "goodplug"
    assert plugin.strategy_cls.strategy_id.startswith("ai_")  # sandbox prefixes it
    assert plugin.granted_permissions == frozenset({"read_candles", "emit_signal"})
    assert hasattr(plugin.strategy_cls, "signal")  # the on_bar_close hook


def test_load_plugin_rejects_sandbox_violation(tmp_path):
    evil = "import os\n" + VALID_STRATEGY  # forbidden import → sandbox rejects
    d = _write_plugin(tmp_path, {"name": "evil", "entry": "strategy.py",
                                 "required_permissions": []}, strategy=evil)
    from vnedge.strategy.ai_sandbox import SandboxViolation

    with pytest.raises(SandboxViolation):
        load_plugin(d)


def test_load_plugins_from_dir_skips_bad_and_keeps_good(tmp_path):
    _write_plugin(tmp_path, {"name": "good", "entry": "strategy.py", "required_permissions": ["compute"]})
    # a plugin that requests trade — manifest rejects it, loader skips it
    _write_plugin(tmp_path, {"name": "bad", "entry": "strategy.py", "required_permissions": ["trade"]})
    loaded = load_plugins_from_dir(tmp_path)
    assert set(loaded) == {"good"}


def test_lifecycle_hook_name_is_documented():
    assert LIFECYCLE_HOOK == "on_bar_close"
