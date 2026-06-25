"""CapsuleEventConsumer — async event source for KGIngestionPipeline.

Supports two event sources:
  1. local-dir  — walks a capsule directory tree, opens each capsule
                  (zip or plain directory), yields events from
                  model-calls.jsonl and tool-calls.jsonl.
  2. nats       — pulls events from a NATS JetStream subject.
                  Gracefully degrades if nats-py is not installed.
"""
from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

# Files within a capsule that carry KG-relevant events
_CAPSULE_EVENT_FILES = ("model-calls.jsonl", "tool-calls.jsonl", "events.jsonl")


class CapsuleEventConsumer:
    """Async iterator that yields event dicts from one or more capsule sources."""

    # ------------------------------------------------------------------
    # Local-dir source
    # ------------------------------------------------------------------

    async def iter_from_directory(
        self, capsule_dir: Path
    ) -> AsyncIterator[dict[str, Any]]:
        """Walk *capsule_dir* recursively and yield events.

        Each capsule may be:
          - A plain directory containing ``model-calls.jsonl`` / ``tool-calls.jsonl``
          - A ``.zip`` file (novafabric capsule archive) containing those files

        Events from every recognised event file are yielded in file order.
        Malformed JSON lines are logged at DEBUG and skipped.
        """
        capsule_dir = Path(capsule_dir)
        if not capsule_dir.is_dir():
            logger.warning("iter_from_directory: %s is not a directory", capsule_dir)
            return

        # Walk for both plain capsule dirs and zip archives
        for path in sorted(capsule_dir.rglob("*")):
            if path.suffix == ".zip" and path.is_file():
                async for event in self._iter_from_zip(path):
                    yield event
            elif path.is_dir():
                for fname in _CAPSULE_EVENT_FILES:
                    events_file = path / fname
                    if events_file.is_file():
                        async for event in self._iter_from_jsonl(events_file):
                            yield event
                        break  # yield from first found file only per directory

    async def _iter_from_jsonl(self, jsonl_path: Path) -> AsyncIterator[dict[str, Any]]:
        """Yield event dicts from a single .jsonl file."""
        try:
            text = jsonl_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("CapsuleEventConsumer: cannot read %s: %s", jsonl_path, exc)
            return
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    yield event
            except json.JSONDecodeError as exc:
                logger.debug("CapsuleEventConsumer: skipping bad JSON in %s: %s", jsonl_path, exc)

    async def _iter_from_zip(self, zip_path: Path) -> AsyncIterator[dict[str, Any]]:
        """Yield event dicts from a capsule zip archive."""
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                for fname in _CAPSULE_EVENT_FILES:
                    # Accept the file at any path depth within the zip
                    matches = [n for n in names if n.endswith("/" + fname) or n == fname]
                    if matches:
                        with zf.open(matches[0]) as fh:
                            for line in fh:
                                line_str = line.decode("utf-8", errors="replace").strip()
                                if not line_str:
                                    continue
                                try:
                                    event = json.loads(line_str)
                                    if isinstance(event, dict):
                                        yield event
                                except json.JSONDecodeError as exc:
                                    logger.debug(
                                        "CapsuleEventConsumer: bad JSON in %s/%s: %s",
                                        zip_path,
                                        matches[0],
                                        exc,
                                    )
                        break  # first matching file only
        except zipfile.BadZipFile as exc:
            logger.warning("CapsuleEventConsumer: bad zip %s: %s", zip_path, exc)
        except OSError as exc:
            logger.warning("CapsuleEventConsumer: cannot open zip %s: %s", zip_path, exc)

    # ------------------------------------------------------------------
    # NATS JetStream source
    # ------------------------------------------------------------------

    async def iter_from_nats(
        self,
        nats_url: str,
        subject: str,
        *,
        batch_size: int = 100,
        timeout: float = 5.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Pull events from a NATS JetStream subject.

        Gracefully degrades when nats-py is not installed: logs a warning and
        returns immediately without yielding any events.

        Args:
            nats_url:   NATS server URL (e.g. ``nats://localhost:4222``).
            subject:    JetStream subject to subscribe to.
            batch_size: Number of messages to fetch per pull request.
            timeout:    Fetch timeout in seconds.
        """
        try:
            import nats  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "CapsuleEventConsumer.iter_from_nats: nats-py not installed. "
                "Install with: pip install nats-py"
            )
            return

        nc = None
        try:
            nc = await nats.connect(nats_url)
            js = nc.jetstream()
            # Push subscribe to pull consumer
            sub = await js.pull_subscribe(subject, durable="nova-kg-consumer")
            while True:
                try:
                    msgs = await sub.fetch(batch=batch_size, timeout=timeout)
                except Exception:
                    # No messages available — end of stream for now
                    break
                for msg in msgs:
                    try:
                        event = json.loads(msg.data.decode("utf-8"))
                        if isinstance(event, dict):
                            yield event
                        await msg.ack()
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        logger.debug(
                            "CapsuleEventConsumer: bad NATS message: %s", exc
                        )
                        await msg.nak()
        except Exception as exc:
            logger.error("CapsuleEventConsumer.iter_from_nats: connection error: %s", exc)
        finally:
            if nc is not None:
                try:
                    await nc.drain()
                except Exception:
                    pass
