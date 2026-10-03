"""Layer resolution and strict policy keys shared by chat and ingestion."""

from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, YamlConfigSettingsSource


class ConfigurationError(Exception):
    """Configuration is missing or invalid without exposing deployment secrets."""


POLICY_DIRECTORY_NAME = "config"


def discover_policy_directory(*, start: Path, override: str | None) -> Path:
    """The explicit override, else the nearest `config/` with a base.yaml above `start`.

    The caller reads its own override variable; this function reads no environment.
    """
    if override:
        return Path(override)
    for parent in start.resolve().parents:
        candidate = parent / POLICY_DIRECTORY_NAME
        if (candidate / "base.yaml").is_file():
            return candidate
    raise ConfigurationError("Policy directory not found; set the policy directory override")


def policy_layers(*, directory: Path, service: str, environment: str) -> list[Path]:
    required = [directory / "base.yaml", directory / f"{environment}.yaml"]
    if any(not path.is_file() for path in required):
        raise ConfigurationError("Missing base or environment policy YAML")
    optional = [
        directory / "services" / f"{service}.yaml",
        directory / "services" / f"{service}.{environment}.yaml",
    ]
    return [*required, *(path for path in optional if path.is_file())]


class PolicyYamlSource(YamlConfigSettingsSource):
    """Only explicitly allowed policy fields can enter a settings schema from YAML."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        *,
        yaml_file: list[Path],
        env_only_fields: frozenset[str],
    ) -> None:
        super().__init__(settings_cls, yaml_file=yaml_file, deep_merge=True)
        self._allowed = {
            name for name, field in settings_cls.model_fields.items() if field.is_required()
        } - env_only_fields

    def __call__(self) -> dict[str, Any]:
        document = super().__call__()
        unknown = sorted(set(document) - self._allowed)
        if unknown:
            raise ConfigurationError(f"YAML keys are not policy fields: {', '.join(unknown)}")
        return document
