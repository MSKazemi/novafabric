"""`nova erasure` must not report a GDPR erasure it did not perform.

Both subcommands were stubs that printed success and did nothing. `request` said
**"GDPR erasure request queued"** and exited 0 — writing no queue row, destroying
no key — even for a run id that did not exist. `status` returned **"pending"** for
any id, including ids never created.

On an Art.17 surface that is the worst possible failure mode: the operator records
an erasure as started and nothing started. A loud failure is strictly better than
a false success, so these now exit non-zero and name the working path.

The real implementations are `nova pii erase` (ADR-0069) and `/v0/erasure`, which
uses the persisted queue in `pii/erasure_queue.py`. Wiring this command to that
queue changes what a compliance command *does* and is an owner decision (ADR-0210).
"""

from __future__ import annotations

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def test_request_does_not_report_a_queued_erasure() -> None:
    result = runner.invoke(app, ["erasure", "request", "--run-id", "does-not-exist"])

    assert result.exit_code != 0, "a no-op must not exit 0 on a GDPR surface"
    assert "queued" not in result.output.lower().replace("never queued", ""), (
        "the word 'queued' as a success claim is exactly the defect"
    )
    assert "not implemented" in result.output.lower()


def test_request_names_the_working_command() -> None:
    """A refusal that does not say what to use instead just relocates the problem."""
    result = runner.invoke(app, ["erasure", "request", "--run-id", "r"])
    assert "nova pii erase" in result.output


def test_status_does_not_report_pending_for_an_unknown_id() -> None:
    result = runner.invoke(app, ["erasure", "status", "--request-id", "never-existed"])

    assert result.exit_code != 0
    assert "status=pending" not in result.output, (
        "reporting 'pending' for an id that was never created is a fabricated state"
    )


def test_the_group_help_does_not_promise_erasure() -> None:
    result = runner.invoke(app, ["erasure", "--help"])
    assert "NOT IMPLEMENTED" in result.output


def test_the_real_erasure_command_still_works() -> None:
    """The redirect must point somewhere real."""
    result = runner.invoke(app, ["pii", "erase", "--help"])

    assert result.exit_code == 0, result.output
    assert "crypto-shredding" in result.output.lower()
