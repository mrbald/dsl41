"""Main-config inputs shared by update and recovery tests (DL-486)."""

from pathlib import Path

from dsl41 import main_config
from dsl41.reload_plan import Estate
from test_main_config import config_file


def write_config(directory: Path, **options) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "non-idempotent.toml").touch(exist_ok=True)
    return config_file(
        directory,
        engine=options.pop("engine", 'machine_policy = "local-eligible"\n'),
        estate='non_idempotent = "non-idempotent.toml"\n' + options.pop("estate", ""),
        **options,
    )


def prepare_estate(directory: Path) -> Estate:
    return main_config.prepare(directory / "dsl41.toml")
