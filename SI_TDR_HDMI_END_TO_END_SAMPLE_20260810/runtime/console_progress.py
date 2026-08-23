from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, TextIO


class _FanoutStream:
    """Write one progress message to the console and both log files."""

    def __init__(self, *streams: TextIO) -> None:
        self._streams = streams

    def write(self, text: str) -> int:
        for stream in self._streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()

    @property
    def encoding(self) -> str:
        return "utf-8"

    def isatty(self) -> bool:
        return False


class ConsoleLogSession:
    """Separate concise progress from verbose Python and Ansys output.

    During the session, ordinary stdout/stderr is redirected to the full log.
    ``progress_stream`` still writes to the original console, the full log, and
    the concise progress log. File-descriptor redirection is used when the
    host stream supports it so native Ansys child processes are captured too.
    """

    def __init__(
        self,
        log_dir: Path,
        *,
        console: TextIO | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.log_dir = Path(log_dir).resolve()
        timestamp = (now or datetime.now)().strftime("%Y%m%d_%H%M%S")
        self.full_log_path = self.log_dir / f"SI_TDR_{timestamp}.log"
        self.progress_log_path = self.log_dir / f"SI_TDR_{timestamp}_progress.log"
        self._requested_console = console
        self._original_stdout: TextIO | None = None
        self._original_stderr: TextIO | None = None
        self._full_log: TextIO | None = None
        self._progress_log: TextIO | None = None
        self._console_stream: TextIO | None = None
        self._saved_stdout_fd: int | None = None
        self._saved_stderr_fd: int | None = None
        self._console_fd_stream: TextIO | None = None
        self.progress_stream: TextIO | None = None

    @staticmethod
    def _stream_fd(stream: TextIO) -> int | None:
        try:
            return stream.fileno()
        except (AttributeError, OSError, ValueError):
            return None

    def __enter__(self) -> "ConsoleLogSession":
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._full_log = self.full_log_path.open(
            "w", encoding="utf-8", buffering=1
        )
        self._progress_log = self.progress_log_path.open(
            "w", encoding="utf-8", buffering=1
        )
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        self._console_stream = self._requested_console or self._original_stdout

        stdout_fd = self._stream_fd(self._original_stdout)
        stderr_fd = self._stream_fd(self._original_stderr)
        full_log_fd = self._stream_fd(self._full_log)
        if (
            self._requested_console is None
            and stdout_fd is not None
            and stderr_fd is not None
            and full_log_fd is not None
        ):
            import os

            self._saved_stdout_fd = os.dup(stdout_fd)
            self._saved_stderr_fd = os.dup(stderr_fd)
            console_fd = os.dup(stdout_fd)
            console_encoding = (
                getattr(self._original_stdout, "encoding", None) or "utf-8"
            )
            self._console_fd_stream = os.fdopen(
                console_fd,
                "w",
                encoding=console_encoding,
                errors="replace",
                buffering=1,
                closefd=True,
            )
            self._console_stream = self._console_fd_stream
            self._original_stdout.flush()
            self._original_stderr.flush()
            os.dup2(full_log_fd, stdout_fd)
            os.dup2(full_log_fd, stderr_fd)

        sys.stdout = self._full_log
        sys.stderr = self._full_log
        self.progress_stream = _FanoutStream(
            self._console_stream,
            self._full_log,
            self._progress_log,
        )
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        import os

        if self.progress_stream is not None:
            self.progress_stream.flush()
        if self._full_log is not None:
            self._full_log.flush()
        if self._progress_log is not None:
            self._progress_log.flush()

        if self._original_stdout is not None:
            sys.stdout = self._original_stdout
        if self._original_stderr is not None:
            sys.stderr = self._original_stderr

        stdout_fd = (
            self._stream_fd(self._original_stdout)
            if self._original_stdout is not None
            else None
        )
        stderr_fd = (
            self._stream_fd(self._original_stderr)
            if self._original_stderr is not None
            else None
        )
        if self._saved_stdout_fd is not None and stdout_fd is not None:
            os.dup2(self._saved_stdout_fd, stdout_fd)
            os.close(self._saved_stdout_fd)
            self._saved_stdout_fd = None
        if self._saved_stderr_fd is not None and stderr_fd is not None:
            os.dup2(self._saved_stderr_fd, stderr_fd)
            os.close(self._saved_stderr_fd)
            self._saved_stderr_fd = None
        if self._console_fd_stream is not None:
            self._console_fd_stream.close()
            self._console_fd_stream = None
        if self._progress_log is not None:
            self._progress_log.close()
        if self._full_log is not None:
            self._full_log.close()


def format_elapsed(seconds: float) -> str:
    """Format a monotonic duration for a human-facing console message."""

    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    whole_seconds = int(round(seconds))
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {remaining_seconds:02d}s"
    return f"{minutes}m {remaining_seconds:02d}s"


@dataclass
class ProgressStage:
    """Mutable result attached to one running console stage."""

    result: str | None = None
    failure: str | None = None

    def complete(self, result: str | None = None) -> None:
        self.result = result

    def fail(self, reason: str) -> None:
        self.failure = reason


class ConsoleProgress:
    """Small, dependency-free progress display for the customer CLI.

    The output intentionally avoids terminal cursor control and ANSI colors so
    it remains readable in PowerShell, EDEN job logs, redirected files, and CI.
    Every line is flushed immediately because Ansys stages can be quiet for
    several minutes.
    """

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        clock: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._stream = stream or sys.stdout
        self._clock = clock or time.monotonic
        self._now = now or datetime.now
        self._run_started: float | None = None

    def _emit(self, message: str) -> None:
        print(message, file=self._stream, flush=True)

    def _prefix(self, area: str, state: str) -> str:
        timestamp = self._now().strftime("%H:%M:%S")
        return f"[{timestamp}] [SI-TDR][{area}][{state}]"

    def _tree_prefix(self, depth: int, marker: str) -> str:
        timestamp = self._now().strftime("%H:%M:%S")
        indent = "    " * max(1, depth)
        return f"[{timestamp}] {indent}{marker}"

    def _stage_prefix(self, area: str, state: str) -> str:
        return f"{self._tree_prefix(1, '>')} [SI-TDR][{area}][{state}]"

    def start_run(
        self,
        config_path: Path,
        operations: list[str],
        *,
        full_log_path: Path | None = None,
        progress_log_path: Path | None = None,
    ) -> None:
        self._run_started = self._clock()
        self._emit("=" * 78)
        self._emit(f"{self._prefix('RUN', 'START')} SI-TDR automation started.")
        self.detail("Input", str(config_path), depth=1)
        self.detail(
            "Requested tasks",
            " -> ".join(operations) if operations else "Prepare run",
            depth=1,
        )
        if full_log_path is not None:
            self.detail("Full log", str(full_log_path), depth=1)
        if progress_log_path is not None:
            self.detail("Progress log", str(progress_log_path), depth=1)
        self._emit("=" * 78)

    def batch(self, index: int, total: int, name: str) -> None:
        self._emit("")
        self._emit("-" * 78)
        self._emit(
            f"{self._prefix('BATCH', f'{index}/{total}')} "
            f"Analysis batch: {name}"
        )
        self._emit("-" * 78)

    @contextmanager
    def stage(
        self,
        area: str,
        label: str,
        *,
        detail: str | None = None,
    ) -> Iterator[ProgressStage]:
        started = self._clock()
        self._emit(f"{self._stage_prefix(area, 'START')} {label}")
        if detail:
            self.detail("Details", detail)
        stage = ProgressStage()
        try:
            yield stage
        except BaseException as exc:
            elapsed = format_elapsed(self._clock() - started)
            reason = str(exc).strip() or type(exc).__name__
            self._emit(
                f"{self._stage_prefix(area, 'FAIL')} {label} failed "
                f"({elapsed})"
            )
            self.detail("Cause", reason)
            raise
        else:
            elapsed = format_elapsed(self._clock() - started)
            if stage.failure:
                self._emit(
                    f"{self._stage_prefix(area, 'FAIL')} {label} failed "
                    f"({elapsed})"
                )
                self.detail("Cause", stage.failure)
            else:
                suffix = f" - {stage.result}" if stage.result else ""
                self._emit(
                    f"{self._stage_prefix(area, 'DONE')} {label} completed "
                    f"({elapsed}){suffix}"
                )

    def info(self, area: str, message: str) -> None:
        self._emit(f"{self._stage_prefix(area, 'INFO')} {message}")

    def warning(self, area: str, message: str) -> None:
        self._emit(f"{self._stage_prefix(area, 'WARN')} {message}")

    def error(self, area: str, message: str) -> None:
        self._emit(f"{self._stage_prefix(area, 'ERROR')} {message}")

    def detail(self, label: str, value: object, *, depth: int = 2) -> None:
        normalized_depth = max(1, depth)
        marker = ">" * normalized_depth
        self._emit(
            f"{self._tree_prefix(normalized_depth, marker)} {label}: {value}"
        )

    def artifact(self, label: str, path: Path | str) -> None:
        artifact_path = Path(path)
        if artifact_path.is_file():
            status = f"file, {artifact_path.stat().st_size:,} bytes"
        elif artifact_path.is_dir():
            status = "directory"
        else:
            status = "missing"
        self.detail(label, f"{artifact_path} [{status}]")

    def finish_run(
        self,
        exit_code: int,
        *,
        result_dirs: list[Path] | None = None,
    ) -> None:
        elapsed = (
            format_elapsed(self._clock() - self._run_started)
            if self._run_started is not None
            else "unknown"
        )
        state = "DONE" if exit_code == 0 else "FAIL"
        message = (
            "All requested tasks completed successfully."
            if exit_code == 0
            else f"Run stopped with exit code {exit_code}."
        )
        self._emit("=" * 78)
        self._emit(f"{self._prefix('RUN', state)} {message} (total {elapsed})")
        for result_dir in result_dirs or []:
            self.detail("Result directory", str(result_dir), depth=1)
        self._emit("=" * 78)
