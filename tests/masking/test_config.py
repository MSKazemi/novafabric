"""masking.yaml loading + validation (ADR-0135 D2, masking-config schema)."""
from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.masking import (
    DEFAULT_MAX_INPUT_BYTES,
    DEFAULT_TIMEOUT_MS,
    MaskingConfigError,
    load_masking_config,
)

VALID_FULL = """\
masking:
  enabled: true
  maskers:
    - id: acme-case-id
      version: "1"
      timeout_ms: 50
      max_input_bytes: 65536
      on_error: redact
      config:
        prefix: "ACME-CASE"
"""

VALID_MINIMAL = """\
masking:
  maskers:
    - id: some-masker
"""

VALID_DISABLED = """\
masking:
  enabled: false
  maskers:
    - id: some-masker
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "masking.yaml"
    p.write_text(text)
    return p


def test_valid_full_config(tmp_path: Path) -> None:
    cfg = load_masking_config(_write(tmp_path, VALID_FULL))
    assert cfg.masking.enabled is True
    (m,) = cfg.masking.maskers
    assert m.id == "acme-case-id"
    assert m.version == "1"
    assert m.timeout_ms == 50
    assert m.max_input_bytes == 65536
    assert m.on_error == "redact"
    assert m.config == {"prefix": "ACME-CASE"}


def test_minimal_config_defaults(tmp_path: Path) -> None:
    cfg = load_masking_config(_write(tmp_path, VALID_MINIMAL))
    assert cfg.masking.enabled is False  # default off: absent switch = ADR-0009
    (m,) = cfg.masking.maskers
    assert m.timeout_ms == DEFAULT_TIMEOUT_MS
    assert m.max_input_bytes == DEFAULT_MAX_INPUT_BYTES
    assert m.on_error == "redact"


def test_disabled_config(tmp_path: Path) -> None:
    cfg = load_masking_config(_write(tmp_path, VALID_DISABLED))
    assert cfg.masking.enabled is False


@pytest.mark.parametrize(
    "text",
    [
        "masking:\n  maskers:\n    - version: '1'\n",  # missing required id
        "masking:\n  maskers:\n    - id: x\n      timeout_ms: 0\n",  # timeout must be > 0
        "masking:\n  maskers:\n    - id: x\n      max_input_bytes: 0\n",
        "masking:\n  maskers:\n    - id: x\n      on_error: explode\n",  # bad enum
        "masking:\n  maskers:\n    - id: x\n      surprise: true\n",  # closed object
        "masking:\n  enabled: true\n",  # maskers[] is required
        "other: {}\n",  # missing masking block
        "- just\n- a\n- list\n",  # not a mapping
        ": : :\n",  # invalid YAML
    ],
)
def test_invalid_configs_fail_closed(tmp_path: Path, text: str) -> None:
    with pytest.raises(MaskingConfigError):
        load_masking_config(_write(tmp_path, text))


def test_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(MaskingConfigError):
        load_masking_config(tmp_path / "does-not-exist.yaml")
