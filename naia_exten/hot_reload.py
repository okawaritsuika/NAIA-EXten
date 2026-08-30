from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable


class FeatureHotReloadWatcher:
    """Polls feature source files and reloads them after a short save debounce."""

    def __init__(
        self,
        *,
        root: Path,
        is_enabled: Callable[[], bool],
        on_reload: Callable[[str], bool],
        log: Callable[[str], None],
        interval: float = 0.45,
        debounce: float = 0.90,
    ):
        self.root = Path(root)
        self.is_enabled = is_enabled
        self.on_reload = on_reload
        self.log = log
        self.interval = max(0.20, float(interval))
        self.debounce = max(0.40, float(debounce))

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, tuple[int, int]] = {}
        self._pending_since: float | None = None
        self._changed_names: set[str] = set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._snapshot = self._take_snapshot()
        self._thread = threading.Thread(
            target=self._run,
            name="naia-exten-feature-hot-reload",
            daemon=True,
        )
        self._thread.start()
        self.log("feature hot reload watcher started")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                current = self._take_snapshot()

                # When auto reload is disabled, track the latest disk state so
                # re-enabling does not unexpectedly apply old edits.
                if not self.is_enabled():
                    self._snapshot = current
                    self._pending_since = None
                    self._changed_names.clear()
                    continue

                changed = self._diff_names(self._snapshot, current)
                if changed:
                    self._snapshot = current
                    self._changed_names.update(changed)
                    self._pending_since = time.monotonic()
                    continue

                if self._pending_since is None:
                    continue
                if time.monotonic() - self._pending_since < self.debounce:
                    continue

                names = sorted(self._changed_names)
                reason = ", ".join(names[:5])
                if len(names) > 5:
                    reason += f" 외 {len(names) - 5}개"

                self._pending_since = None
                self._changed_names.clear()
                self.on_reload(reason or "feature 파일 변경")

                # Reload/import may create __pycache__; source snapshot should
                # remain based only on *.py files.
                self._snapshot = self._take_snapshot()
            except Exception as exc:
                self.log(f"feature hot reload watcher error: {exc}")

    def _take_snapshot(self) -> dict[str, tuple[int, int]]:
        snapshot: dict[str, tuple[int, int]] = {}
        if not self.root.is_dir():
            return snapshot

        for path in self.root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(self.root).as_posix()
            snapshot[rel] = (int(stat.st_mtime_ns), int(stat.st_size))
        return snapshot

    @staticmethod
    def _diff_names(
        before: dict[str, tuple[int, int]],
        after: dict[str, tuple[int, int]],
    ) -> set[str]:
        names = set(before) | set(after)
        return {name for name in names if before.get(name) != after.get(name)}
