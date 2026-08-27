# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""``/api/cost/report`` must answer from the capsules when DuckDB is empty.

The DuckDB accumulator is the cluster-scale projection: its only writers (the
NATS consumer and the collector app) both open it at ``:memory:``.  Nothing
populates the on-disk file in local mode, so the endpoint reported ``$0.0000``
and ``0`` model calls for runs whose capsules carried exact ``gen_ai.usage.*``
counts — while its own docstring promised cost data "without external
infrastructure".  These tests pin the capsule fallback and the honesty of the
``priced`` / ``unpriced_models`` fields.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from novafabric.serve.app import _cost_report_from_capsules


def _write_calls(root: Path, run_id: str, records: list[dict[str, object]]) -> None:
    cdir = root / run_id
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "model-calls.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def _call(
    model: str,
    tok_in: int,
    tok_out: int,
    *,
    age_days: int = 0,
) -> dict[str, object]:
    when = datetime.now(tz=timezone.utc) - timedelta(days=age_days)
    return {
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.usage.input_tokens": tok_in,
        "gen_ai.usage.output_tokens": tok_out,
        "started_at": when.isoformat().replace("+00:00", "Z"),
    }


def test_aggregates_tokens_and_calls_per_model(tmp_path: Path) -> None:
    _write_calls(tmp_path, "run-a", [_call("gpt-4o-mini", 42, 77)])
    _write_calls(tmp_path, "run-b", [_call("gpt-4o-mini", 10, 5)])

    report = _cost_report_from_capsules(tmp_path, days=7)

    assert set(report) == {"gpt-4o-mini"}
    entry = report["gpt-4o-mini"]
    assert entry["calls"] == 2
    assert entry["tokens_in"] == 52
    assert entry["tokens_out"] == 82


def test_cost_uses_the_shipped_price_table(tmp_path: Path) -> None:
    from novafabric.cost.interceptor import CostInterceptor

    inp, outp = CostInterceptor.PRICE_TABLE["gpt-4o-mini"]
    _write_calls(tmp_path, "run-a", [_call("gpt-4o-mini", 1000, 2000)])

    entry = _cost_report_from_capsules(tmp_path, days=7)["gpt-4o-mini"]

    assert entry["priced"] is True
    assert entry["cost_usd"] == round(inp * 1.0 + outp * 2.0, 6)


def test_unpriced_model_is_flagged_not_reported_as_free(tmp_path: Path) -> None:
    """A model absent from the price table must not pass a zero off as a price."""
    _write_calls(tmp_path, "run-a", [_call("some-model-with-no-published-price", 9, 9)])

    entry = _cost_report_from_capsules(tmp_path, days=7)[
        "some-model-with-no-published-price"
    ]

    assert entry["priced"] is False
    assert entry["cost_usd"] == 0.0
    # The usage itself is still real and must survive.
    assert entry["calls"] == 1
    assert entry["tokens_in"] == 9


def test_run_id_filter_and_window(tmp_path: Path) -> None:
    _write_calls(tmp_path, "run-a", [_call("gpt-4o-mini", 1, 1)])
    _write_calls(tmp_path, "run-b", [_call("gpt-4o-mini", 100, 100)])
    _write_calls(tmp_path, "run-old", [_call("gpt-4o-mini", 500, 500, age_days=99)])

    only_a = _cost_report_from_capsules(tmp_path, days=7, run_id="run-a")
    assert only_a["gpt-4o-mini"]["tokens_in"] == 1

    windowed = _cost_report_from_capsules(tmp_path, days=7)
    assert windowed["gpt-4o-mini"]["tokens_in"] == 101  # run-old excluded

    wide = _cost_report_from_capsules(tmp_path, days=365)
    assert wide["gpt-4o-mini"]["tokens_in"] == 601


def test_tolerates_missing_dirs_bad_lines_and_nova_usage(tmp_path: Path) -> None:
    (tmp_path / "not-a-run.txt").write_text("x", encoding="utf-8")
    (tmp_path / "empty-run").mkdir()
    cdir = tmp_path / "run-a"
    cdir.mkdir()
    (cdir / "model-calls.jsonl").write_text(
        "\n".join(
            [
                "",
                "{not json",
                json.dumps({"gen_ai.request.model": ""}),  # no model -> skipped
                json.dumps(
                    {
                        "gen_ai.request.model": "gpt-4o",
                        "started_at": "not-a-timestamp",
                        "nova.usage": {"input_tokens": 7, "output_tokens": 3},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = _cost_report_from_capsules(tmp_path, days=7)

    assert report["gpt-4o"]["calls"] == 1
    assert report["gpt-4o"]["tokens_in"] == 7
    assert report["gpt-4o"]["tokens_out"] == 3


def test_missing_capsule_root_is_empty_not_an_error(tmp_path: Path) -> None:
    assert _cost_report_from_capsules(tmp_path / "nope", days=7) == {}


# --------------------------------------------------------------------------
# End-to-end: the endpoint must actually reach the fallback.
# --------------------------------------------------------------------------

import pytest  # noqa: E402

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

_TOKEN = "test-token-1234567890abcdef"
_HEADERS = {"host": "127.0.0.1:4321", "Authorization": f"Bearer {_TOKEN}"}


def test_endpoint_reports_real_usage_from_capsules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "runs"
    base.mkdir()
    _write_calls(base, "run-a", [_call("gpt-4o-mini", 1000, 2000)])
    # Point DuckDB at a path that cannot hold anything, mirroring local mode
    # where the accumulator's only writers open it at ``:memory:``.
    monkeypatch.setenv(
        "NOVA_EVIDENCE_DUCKDB_PATH", str(tmp_path / "absent" / "evidence.duckdb")
    )
    monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)

    app = create_app(
        token=_TOKEN, capsule_dir=base, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as client:
        res = client.get("/api/cost/report?days=7", headers=_HEADERS)

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["backend"] == "capsules"
    assert body["totals"]["input_tokens"] == 1000
    assert body["totals"]["completion_tokens"] == 2000
    assert body["totals"]["cost_usd"] > 0.0
    assert body["unpriced_models"] == []
    assert body["by_model"][0]["model_id"] == "gpt-4o-mini"
    assert body["by_model"][0]["calls"] == 1


def test_endpoint_flags_unpriced_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "runs"
    base.mkdir()
    _write_calls(base, "run-a", [_call("model-with-no-published-price", 42, 77)])
    monkeypatch.setenv(
        "NOVA_EVIDENCE_DUCKDB_PATH", str(tmp_path / "absent" / "evidence.duckdb")
    )
    monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)

    app = create_app(
        token=_TOKEN, capsule_dir=base, db_path=tmp_path / "r.db", static_dir=None
    )
    with TestClient(app) as client:
        body = client.get("/api/cost/report?days=7", headers=_HEADERS).json()

    # Usage is real even though the price is unknown; $0.00 must not read as free.
    assert body["totals"]["input_tokens"] == 42
    assert body["totals"]["completion_tokens"] == 77
    assert body["unpriced_models"] == ["model-with-no-published-price"]
    assert body["by_model"][0]["priced"] is False
