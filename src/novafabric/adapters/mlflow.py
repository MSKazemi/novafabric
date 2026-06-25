from __future__ import annotations

import os


def validate_run_id(run_id: str) -> bool | None:
    if not os.environ.get("MLFLOW_TRACKING_URI"):
        return None
    return True
