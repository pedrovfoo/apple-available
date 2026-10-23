import pytest
from pathlib import Path
from watcher.config import load_config, ConfigError


def _cfg(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(Path("config.toml").read_text())
    return p


def test_loads_all_fields(tmp_path):
    cfg = load_config(_cfg(tmp_path),
                      {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "-1004333816460"})
    assert cfg.zip == "19702"
    assert [(w.model, w.capacity, w.colors) for w in cfg.watch] == [
        ("iPhone 18 Pro Max", "256GB", ["*"]),
        ("iPhone 18 Pro Max", "1TB", ["Burgundy"]),
        ("iPhone Duo", "256GB", ["*"]),
        ("iPhone Duo", "512GB", ["*"]),
        ("iPhone Duo", "1TB", ["*"]),
        ("iPhone Duo", "2TB", ["*"]),
    ]
    assert cfg.canary_part == "MJQ34LL/A"
    assert cfg.chat_id == "-1004333816460"
    assert cfg.bot_token == "tok"
    assert cfg.repeat_minutes == 30
    assert cfg.failure_alert_threshold == 5


def test_missing_token_raises(tmp_path):
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_config(_cfg(tmp_path), {})


def test_missing_chat_id_raises(tmp_path):
    """config.toml no longer carries the chat id, so the env must supply it."""
    with pytest.raises(ConfigError, match="TELEGRAM_CHAT_ID"):
        load_config(_cfg(tmp_path), {"TELEGRAM_BOT_TOKEN": "tok"})


def test_chat_id_may_still_come_from_config_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[[watch]]\nmodel = "M"\ncapacity = "256GB"\ncolors = ["*"]\n'
        '[location]\nzip = "1"\n[telegram]\nchat_id = "-42"\n'
        '[canary]\npart = "X"\n'
    )
    assert load_config(p, {"TELEGRAM_BOT_TOKEN": "t"}).chat_id == "-42"


def test_env_chat_id_overrides_file(tmp_path):
    cfg = load_config(_cfg(tmp_path), {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "-999"})
    assert cfg.chat_id == "-999"


def test_legacy_single_watch_table_still_loads(tmp_path):
    """A pre-existing [watch] table must keep working after the move to [[watch]]."""
    p = tmp_path / "config.toml"
    p.write_text(
        '[watch]\nmodel = "iPhone 18 Pro Max"\ncapacity = "256GB"\ncolors = ["*"]\n'
        '[location]\nzip = "33130"\n[telegram]\nchat_id = "-1"\n'
        '[canary]\npart = "MJQ34LL/A"\n'
    )
    cfg = load_config(p, {"TELEGRAM_BOT_TOKEN": "t"})
    assert len(cfg.watch) == 1
    assert cfg.watch[0].capacity == "256GB"


def test_no_watch_targets_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[location]\nzip = "1"\n[telegram]\nchat_id = "-1"\n'
                 '[canary]\npart = "X"\n')
    with pytest.raises(ConfigError, match="watching nothing"):
        load_config(p, {"TELEGRAM_BOT_TOKEN": "t"})


def test_multiple_targets_are_all_loaded(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[[watch]]\nmodel = "iPhone 18 Pro Max"\ncapacity = "256GB"\ncolors = ["*"]\n'
        '[[watch]]\nmodel = "iPhone 18 Pro Max"\ncapacity = "2TB"\ncolors = ["Black"]\n'
        '[[watch]]\nmodel = "iPhone 18 Pro"\ncapacity = "512GB"\ncolors = ["*"]\n'
        '[location]\nzip = "33130"\n[telegram]\nchat_id = "-1"\n'
        '[canary]\npart = "MJQ34LL/A"\n'
    )
    cfg = load_config(p, {"TELEGRAM_BOT_TOKEN": "t"})
    assert len(cfg.watch) == 3
    assert cfg.watch[1].colors == ["Black"]
    assert str(cfg.watch[0]) == "iPhone 18 Pro Max 256GB (all colors)"
