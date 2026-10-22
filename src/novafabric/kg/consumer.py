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
import os
import zipfile
from pathlib import Path
from typing import Any, AsyncIterator

from novafabric._capsule_fs import (
    CapsuleEntryError,
    is_capsule_regular_file,
    open_capsule_file,
    read_capsule_text_or_none,
)
from novafabric.capture.record_roles import non_counting_model_call_ids

logger = logging.getLogger(__name__)

# Files within a capsule that carry KG-relevant events
_CAPSULE_EVENT_FILES = ("model-calls.jsonl", "tool-calls.jsonl", "events.jsonl")


def _walk_no_follow(base: Path) -> list[Path]:
    """Every real directory and ``*.zip`` file under *base* (not *base* itself), sorted.

    ``os.walk(followlinks=False)`` never descends into a linked directory, and a
    link entry is never returned, so the walk cannot leave *base*.
    """
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        here = Path(dirpath)
        for name in dirnames:
            if not (here / name).is_symlink():
                out.append(here / name)
        for name in filenames:
            if name.endswith(".zip"):
                out.append(here / name)
    return sorted(out)


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

        # Walk for both plain capsule dirs and zip archives. No link is followed:
        # os.walk does not descend into a linked directory, a linked directory
        # or zip is not a candidate, and every file is opened through
        # novafabric._capsule_fs (O_NOFOLLOW, regular files only).
        for path in _walk_no_follow(capsule_dir):
            if path.suffix == ".zip":
                if is_capsule_regular_file(path.parent, path.name):
                    async for event in self._iter_from_zip(path):
                        yield event
                continue
            for fname in _CAPSULE_EVENT_FILES:
                if is_capsule_regular_file(path, fname):
                    async for event in self._iter_from_jsonl(path / fname):
                        yield event
                    break  # yield from first found file only per directory

    async def _iter_from_jsonl(self, jsonl_path: Path) -> AsyncIterator[dict[str, Any]]:
        """Yield event dicts from a single .jsonl file (never through a link)."""
        try:
            text = read_capsule_text_or_none(
                jsonl_path.parent, jsonl_path.name, operation="knowledge-graph ingest"
            )
        except OSError as exc:
            logger.warning("CapsuleEventConsumer: cannot read %s: %s", jsonl_path, exc)
            return
        if text is None:
            return
        # ADR-0305: transport records (incl. pre-ADR-0305 duplicates) are not calls.
        transport = (
            non_counting_model_call_ids(jsonl_path)
            if jsonl_path.name == "model-calls.jsonl" else set()
        )
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict) and event.get("model_call_id") not in transport:
                    yield event
            except json.JSONDecodeError as exc:
                logger.debug("CapsuleEventConsumer: skipping bad JSON in %s: %s", jsonl_path, exc)

    async def _iter_from_zip(self, zip_path: Path) -> AsyncIterator[dict[str, Any]]:
        """Yield event dicts from a capsule zip archive."""
        try:
            with (
                open_capsule_file(
                    zip_path.parent, zip_path.name, operation="knowledge-graph ingest"
                ) as raw,
                zipfile.ZipFile(raw, "r") as zf,
            ):
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
        except CapsuleEntryError as exc:
            logger.warning("CapsuleEventConsumer: zip not followed %s: %s", zip_path, exc)
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
