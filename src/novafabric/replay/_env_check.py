from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from typing import Any


@dataclass
class EnvWarning:
    field: str
    recorded: str
    current: str
    severity: str  # "minor" | "major"
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "recorded": self.recorded,
            "current": self.current,
            "severity": self.severity,
            "message": self.message,
        }


class EnvironmentResolver:
    """Compare recorded env.lock fields against the current runtime environment."""

    def check(self, env_lock: dict[str, Any]) -> list[EnvWarning]:
        warnings: list[EnvWarning] = []
        self._check_python(env_lock, warnings)
        self._check_os(env_lock, warnings)
        return warnings

    def _check_python(self, env_lock: dict[str, Any], warnings: list[EnvWarning]) -> None:
        recorded_py = env_lock.get("python", {}).get("version", "")
        current_py = platform.python_version()
        if recorded_py and recorded_py != current_py:
            recorded_parts = recorded_py.split(".")
            current_parts = current_py.split(".")
            severity = "major" if recorded_parts[:2] != current_parts[:2] else "minor"
            warnings.append(EnvWarning(
                field="python.version",
                recorded=recorded_py,
                current=current_py,
                severity=severity,
                message=f"Python version changed: {recorded_py} → {current_py}",
            ))

        recorded_impl = env_lock.get("python", {}).get("interpreter", "")
        current_impl = platform.python_implementation().lower()
        current_impl_norm = "cpython" if current_impl == "cpython" else "pypy3"
        if recorded_impl and recorded_impl != current_impl_norm:
            warnings.append(EnvWarning(
                field="python.interpreter",
                recorded=recorded_impl,
                current=current_impl_norm,
                severity="major",
                message=f"Python interpreter changed: {recorded_impl} → {current_impl_norm}",
            ))

    def _check_os(self, env_lock: dict[str, Any], warnings: list[EnvWarning]) -> None:
        recorded_os = env_lock.get("host", {}).get("os", "")
        current_os = sys.platform
        current_os_norm = (
            "linux" if current_os.startswith("linux") else
            "darwin" if current_os == "darwin" else
            "windows" if current_os.startswith("win") else
            "linux"
        )
        if recorded_os and recorded_os != current_os_norm:
            warnings.append(EnvWarning(
                field="host.os",
                recorded=recorded_os,
                current=current_os_norm,
                severity="major",
                message=f"OS changed: {recorded_os} → {current_os_norm}",
            ))
