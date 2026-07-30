"""`nova lineage consume` CLI smoke tests (cap-006, ADR-0061/0066/0219)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def test_help_smoke() -> None:
    result = runner.invoke(app, ["lineage", "consume", "--help"])
    assert result.exit_code == 0
    assert "LineageConsumer" in result.output
    assert "--flush-batch-size" in result.output
    assert "--flush-interval-s" in result.output


def test_invokes_run_from_nats_with_cli_args() -> None:
    """The CLI wires its flags straight through to run_from_nats()."""
    with patch(
        "novafabric.lineage.consumer.LineageConsumer.run_from_nats",
        new_callable=AsyncMock,
    ) as mock_run:
        result = runner.invoke(
            app,
            [
                "lineage",
                "consume",
                "--nats-url",
                "nats://test:4222",
                "--subject",
                "custom.subject.>",
                "--batch-size",
                "100",
                "--fetch-timeout",
                "2.5",
                "--flush-batch-size",
                "3000",
                "--flush-interval-s",
                "30",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_run.assert_called_once_with(
        subject="custom.subject.>",
        batch_size=100,
        fetch_timeout=2.5,
        flush_batch_size=3000,
        flush_interval_s=30.0,
    )


def test_keyboard_interrupt_exits_cleanly() -> None:
    """Ctrl-C during run_from_nats() must not crash the CLI with a traceback."""
    with patch(
        "novafabric.lineage.consumer.LineageConsumer.run_from_nats",
        new_callable=AsyncMock,
        side_effect=KeyboardInterrupt,
    ):
        result = runner.invoke(app, ["lineage", "consume"])

    assert result.exit_code == 0
    assert "stopped" in result.output.lower()


def test_propagates_missing_nats_url_error() -> None:
    """A genuine configuration error (no NATS URL) surfaces, not swallowed."""
    with patch(
        "novafabric.lineage.consumer.LineageConsumer.run_from_nats",
        new_callable=AsyncMock,
        side_effect=RuntimeError("NOVA_NATS_URL not set"),
    ):
        result = runner.invoke(app, ["lineage", "consume"])

    assert result.exit_code != 0
