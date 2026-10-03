"""Policy directory discovery: an explicit override wins, else the nearest config/ above."""

from pathlib import Path

import pytest

from horizon_config import ConfigurationError, discover_policy_directory


def tree(root: Path) -> Path:
    (root / "config").mkdir()
    (root / "config" / "base.yaml").write_text("{}\n")
    module = root / "services" / "svc" / "src" / "pkg" / "settings.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    return module


def test_nearest_config_directory_with_a_base_policy_is_found(tmp_path: Path) -> None:
    module = tree(tmp_path)
    (module.parent / "config").mkdir()  # no base.yaml: not a policy directory
    assert discover_policy_directory(start=module, override=None) == tmp_path / "config"


def test_override_wins_without_touching_the_filesystem(tmp_path: Path) -> None:
    module = tree(tmp_path)
    assert discover_policy_directory(start=module, override="/etc/horizon") == Path("/etc/horizon")


@pytest.mark.parametrize("override", [None, ""])
def test_missing_policy_directory_is_a_configuration_error(
    tmp_path: Path, override: str | None
) -> None:
    module = tmp_path / "pkg" / "settings.py"
    module.parent.mkdir()
    module.write_text("")
    with pytest.raises(ConfigurationError, match="Policy directory not found"):
        discover_policy_directory(start=module, override=override)
