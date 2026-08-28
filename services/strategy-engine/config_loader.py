import tomllib
from pathlib import Path


class StrategyConfigError(ValueError):
    pass


def load_strategy_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.is_file():
        raise StrategyConfigError(f"Strategy configuration file not found: {config_path}")

    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise StrategyConfigError(f"Invalid strategy configuration: {config_path}") from exc

    if not isinstance(config, dict):
        raise StrategyConfigError("Strategy configuration root must be a TOML table")
    for section in ("risk", "swing", "intraday", "defined_risk_options"):
        value = config.get(section, {})
        if not isinstance(value, dict):
            raise StrategyConfigError(f"Strategy configuration section [{section}] must be a table")
    return config
