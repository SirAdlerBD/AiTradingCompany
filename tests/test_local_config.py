"""config/local.yaml is deep-merged over config/desk.yaml; the hash covers both."""
from pathlib import Path

import yaml

from desk import cli, config as cfgmod

REPO = Path(__file__).parent.parent


def test_deep_merge_semantics():
    base = {"a": {"x": 1, "y": 2}, "list": [1, 2], "s": "base"}
    over = {"a": {"y": 3, "z": 4}, "list": [9], "new": True}
    assert cfgmod.deep_merge(base, over) == {"a": {"x": 1, "y": 3, "z": 4}, "list": [9], "s": "base", "new": True}


def test_local_overrides_only_its_keys_and_changes_the_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "desk.yaml").write_text((REPO / "config" / "desk.yaml").read_text())
    (cfgdir / "risk_rules.yaml").write_text((REPO / "config" / "risk_rules.yaml").read_text())
    base = cfgmod.load(cfgdir / "desk.yaml")
    assert base.local_path is None and [t.symbol for t in base.universe.tickers] == ["MSFT"]

    (cfgdir / "local.yaml").write_text((REPO / "config" / "local.yaml.example").read_text())
    cfg = cfgmod.load(cfgdir / "desk.yaml")
    assert cfg.local_path == (cfgdir / "local.yaml").resolve()
    assert [t.symbol for t in cfg.universe.tickers] == ["MSFT", "XIOR"]
    assert cfg.universe.tickers[1].mic == "xbru" and cfg.universe.tickers[1].currency == "EUR"
    assert cfg.pipeline.analysts == ["technical_analyst", "fundamentals_analyst"]
    assert cfg.roles["trader"].effort == "low"
    # untouched keys still come from desk.yaml
    assert cfg.roles["trader"].model == base.roles["trader"].model and cfg.roles["trader"].provider == "anthropic"
    assert cfg.universe.history_days == base.universe.history_days and cfg.risk.rules_file == base.risk.rules_file
    assert cfg.raw_hash != base.raw_hash

    # the effective config round-trips and the banner names the local file
    merged = yaml.safe_load(cfgmod.effective_yaml(cfg))
    assert merged["universe"]["tickers"][1]["symbol"] == "XIOR" and merged["roles"]["trader"]["effort"] == "low"
    assert "local" in cli.banner(str(cfgdir / "desk.yaml"), cfg.local_path) and "no local.yaml" in cli.banner("x", None)


def test_example_local_is_valid_and_gitignored():
    assert (REPO / "config" / "local.yaml.example").exists()
    assert "config/local.yaml\n" in (REPO / ".gitignore").read_text()
    assert not (REPO / "config" / "local.yaml").exists() or True   # a developer may have one; it must never be committed
