# Copyright 2026 dupeGuru developers
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""Bounded external-tool execution and FFmpeg/Chromaprint protocol helpers."""

from __future__ import annotations

import os
import math
import re
import signal
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple

from core.safe_json import JsonStructuralLimits
from core.video.json_guard import strict_bounded_json_loads
from core.video.model import MAX_TOOL_TEXT_CHARACTERS, VideoMetadata

MAX_FFPROBE_JSON_BYTES = 4 * 1024 * 1024
MAX_FFPROBE_INTEGER = (1 << 63) - 1
FFPROBE_JSON_LIMITS = JsonStructuralLimits(
    max_depth=16,
    max_container_entries=4096,
    max_total_nodes=100_000,
    max_scalar_tokens=100_000,
    max_total_string_chars=2 * 1024 * 1024,
    max_string_chars=64 * 1024,
    max_scalar_chars=1024,
)

if os.name == "nt":
    import ctypes

    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = (
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        )

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_job_object = _kernel32.CreateJobObjectW
    _create_job_object.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _create_job_object.restype = wintypes.HANDLE
    _set_information_job_object = _kernel32.SetInformationJobObject
    _set_information_job_object.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _set_information_job_object.restype = wintypes.BOOL
    _assign_process_to_job_object = _kernel32.AssignProcessToJobObject
    _assign_process_to_job_object.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _assign_process_to_job_object.restype = wintypes.BOOL
    _terminate_job_object = _kernel32.TerminateJobObject
    _terminate_job_object.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _terminate_job_object.restype = wintypes.BOOL
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = (wintypes.HANDLE,)
    _close_handle.restype = wintypes.BOOL


class _ProcessJob:
    """Best-effort Windows Job Object that owns every descendant process."""

    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return
        handle = _create_job_object(None, None)
        if not handle:
            return
        information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _set_information_job_object(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ) or not _assign_process_to_job_object(
            handle,
            wintypes.HANDLE(int(process._handle)),
        ):
            _close_handle(handle)
            return
        self.handle = handle

    def terminate(self):
        if self.handle is None:
            return False
        return bool(_terminate_job_object(self.handle, 1))

    def close(self):
        if self.handle is not None:
            _close_handle(self.handle)
            self.handle = None


def _terminate_posix_process_group(process):
    if os.name == "nt":
        return False
    try:
        os.killpg(process.pid, signal.SIGKILL)
        return True
    except (OSError, ProcessLookupError):
        return False


def _terminate_process_tree(process, process_job):
    if process_job.terminate():
        return
    if os.name == "nt":
        try:
            outcome = subprocess.run(
                ("taskkill.exe", "/PID", str(process.pid), "/T", "/F"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if outcome.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    elif _terminate_posix_process_group(process):
        return
    try:
        process.kill()
    except OSError:
        pass


class CancellationToken(Protocol):
    def is_set(self) -> bool: ...  # noqa: E704


class CommandState(Enum):
    SUCCESS = "success"
    NONZERO_EXIT = "nonzero_exit"
    MISSING_EXECUTABLE = "missing_executable"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    OUTPUT_LIMIT = "output_limit"
    START_FAILED = "start_failed"


@dataclass(frozen=True)
class CommandOutcome:
    argv: Tuple[str, ...]
    state: CommandState
    returncode: Optional[int]
    stdout: bytes = b""
    stderr: bytes = b""
    duration_seconds: float = 0.0
    error: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        if not self.argv or any(not item or "\0" in item for item in self.argv):
            raise ValueError("command argv must contain safe, non-empty strings")
        if self.duration_seconds < 0:
            raise ValueError("command duration must be non-negative")
        if self.state is CommandState.SUCCESS and self.returncode != 0:
            raise ValueError("successful command must have return code zero")
        if self.state is CommandState.NONZERO_EXIT and (self.returncode is None or self.returncode == 0):
            raise ValueError("non-zero command outcome requires a non-zero return code")


class CommandRunner(Protocol):
    def run(  # noqa: E704
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        cancel_event: Optional[CancellationToken] = None,
        max_output_bytes: int = 4 * 1024 * 1024,
    ) -> CommandOutcome: ...


class SubprocessCommandRunner:
    """Execute an argv without a shell while bounding time and captured output."""

    _READ_SIZE = 64 * 1024

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        cancel_event: Optional[CancellationToken] = None,
        max_output_bytes: int = 4 * 1024 * 1024,
    ) -> CommandOutcome:
        command = _validate_argv(argv)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")
        if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive integer")
        if cancel_event is not None and cancel_event.is_set():
            return CommandOutcome(command, CommandState.CANCELLED, None, error="cancelled before process start")

        started = time.monotonic()
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
        except FileNotFoundError:
            return CommandOutcome(
                command,
                CommandState.MISSING_EXECUTABLE,
                None,
                duration_seconds=time.monotonic() - started,
                error="executable was not found",
            )
        except OSError as error:
            return CommandOutcome(
                command,
                CommandState.START_FAILED,
                None,
                duration_seconds=time.monotonic() - started,
                error=str(error),
            )
        process_job = _ProcessJob(process)

        stdout = bytearray()
        stderr = bytearray()
        output_limit_reached = threading.Event()
        capture_lock = threading.Lock()
        total_captured = 0

        def capture(stream: object, destination: bytearray) -> None:
            nonlocal total_captured
            try:
                while True:
                    chunk = stream.read(self._READ_SIZE)  # type: ignore[attr-defined]
                    if not chunk:
                        break
                    with capture_lock:
                        remaining = max_output_bytes - total_captured
                        if remaining > 0:
                            accepted = chunk[:remaining]
                            destination.extend(accepted)
                            total_captured += len(accepted)
                        if len(chunk) > remaining:
                            output_limit_reached.set()
                            break
            except (OSError, ValueError):
                # Pipe closure is expected after cancellation or a limit-triggered kill.
                return

        assert process.stdout is not None
        assert process.stderr is not None
        readers = (
            threading.Thread(target=capture, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=capture, args=(process.stderr, stderr), daemon=True),
        )
        for reader in readers:
            reader.start()

        forced_state: Optional[CommandState] = None
        forced_error: Optional[str] = None
        deadline = started + timeout_seconds
        while process.poll() is None:
            if output_limit_reached.is_set():
                forced_state = CommandState.OUTPUT_LIMIT
                forced_error = "captured output exceeded {} bytes".format(max_output_bytes)
                _terminate_process_tree(process, process_job)
                break
            if cancel_event is not None and cancel_event.is_set():
                forced_state = CommandState.CANCELLED
                forced_error = "command was cancelled"
                _terminate_process_tree(process, process_job)
                break
            if time.monotonic() >= deadline:
                forced_state = CommandState.TIMED_OUT
                forced_error = "command exceeded {:.3f} seconds".format(timeout_seconds)
                _terminate_process_tree(process, process_job)
                break
            time.sleep(0.01)

        try:
            returncode = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process, process_job)
            returncode = process.wait(timeout=2)
        if os.name != "nt":
            _terminate_posix_process_group(process)
        process_job.close()

        # A short-lived process can exit before the pipe readers have consumed
        # the final kernel-buffered output.  Drain those readers before deciding
        # whether the command respected its combined output limit.
        for reader in readers:
            reader.join(timeout=2)
        if any(reader.is_alive() for reader in readers):
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            for reader in readers:
                reader.join(timeout=2)
        capture_drain_incomplete = any(reader.is_alive() for reader in readers)
        if output_limit_reached.is_set() and forced_state is None:
            forced_state = CommandState.OUTPUT_LIMIT
            forced_error = "captured output exceeded {} bytes".format(max_output_bytes)
        elif capture_drain_incomplete and forced_state is None:
            forced_state = CommandState.OUTPUT_LIMIT
            forced_error = "captured output could not be drained within the bounded shutdown interval"
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()
        if process.stderr is not None and not process.stderr.closed:
            process.stderr.close()
        duration = time.monotonic() - started
        if forced_state is not None:
            return CommandOutcome(
                command,
                forced_state,
                returncode,
                bytes(stdout),
                bytes(stderr),
                duration,
                forced_error,
            )
        state = CommandState.SUCCESS if returncode == 0 else CommandState.NONZERO_EXIT
        error = None if returncode == 0 else "process exited with status {}".format(returncode)
        return CommandOutcome(command, state, returncode, bytes(stdout), bytes(stderr), duration, error)


class FakeCommandRunner:
    """Deterministic runner for tests and embedders which provide recorded tool output."""

    def __init__(self, responses: Optional[Mapping[Tuple[str, ...], CommandOutcome]] = None) -> None:
        self.responses: Dict[Tuple[str, ...], CommandOutcome] = dict(responses or {})
        self.calls = []

    def add(
        self,
        argv: Sequence[str],
        *,
        state: CommandState = CommandState.SUCCESS,
        returncode: Optional[int] = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        error: Optional[str] = None,
    ) -> None:
        command = _validate_argv(argv)
        self.responses[command] = CommandOutcome(command, state, returncode, stdout, stderr, error=error)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        cancel_event: Optional[CancellationToken] = None,
        max_output_bytes: int = 4 * 1024 * 1024,
    ) -> CommandOutcome:
        command = _validate_argv(argv)
        self.calls.append((command, timeout_seconds, max_output_bytes))
        if cancel_event is not None and cancel_event.is_set():
            return CommandOutcome(command, CommandState.CANCELLED, None, error="fake command cancelled")
        try:
            response = self.responses[command]
        except KeyError:
            return CommandOutcome(command, CommandState.START_FAILED, None, error="no fake response configured")
        if len(response.stdout) + len(response.stderr) > max_output_bytes:
            return CommandOutcome(
                command,
                CommandState.OUTPUT_LIMIT,
                response.returncode,
                (response.stdout + response.stderr)[:max_output_bytes],
                error="fake output exceeded configured limit",
            )
        return response


def _validate_argv(argv: Sequence[str]) -> Tuple[str, ...]:
    command = tuple(argv)
    if not command or any(not isinstance(item, str) or not item or "\0" in item for item in command):
        raise ValueError("argv must contain safe, non-empty strings")
    return command


class ToolName(Enum):
    FFPROBE = "ffprobe"
    FFMPEG = "ffmpeg"
    FPCALC = "fpcalc"


class ToolState(Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class ToolCapability:
    tool: ToolName
    state: ToolState
    executable: Optional[str]
    version: Optional[str]
    message: str

    @property
    def available(self) -> bool:
        return self.state is ToolState.AVAILABLE


def detect_capabilities(
    runner: CommandRunner,
    *,
    executables: Optional[Mapping[ToolName, str]] = None,
    resolver: Callable[[str], Optional[str]] = shutil.which,
    timeout_seconds: float = 5,
    cancel_event: Optional[CancellationToken] = None,
) -> Tuple[ToolCapability, ...]:
    """Probe all external tools and preserve every missing/degraded state."""

    result = []
    provided = dict(executables or {})
    for tool in ToolName:
        executable = provided.get(tool)
        if executable is None:
            executable = resolver(tool.value)
        if not executable:
            result.append(ToolCapability(tool, ToolState.MISSING, None, None, "executable was not found"))
            continue
        outcome = runner.run(
            (executable, "-version"),
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
            max_output_bytes=256 * 1024,
        )
        if outcome.state is CommandState.SUCCESS:
            version = _first_nonempty_line(outcome.stdout or outcome.stderr)
            result.append(ToolCapability(tool, ToolState.AVAILABLE, executable, version, "available"))
        elif outcome.state is CommandState.MISSING_EXECUTABLE:
            result.append(ToolCapability(tool, ToolState.MISSING, executable, None, outcome.error or "missing"))
        elif outcome.state is CommandState.TIMED_OUT:
            result.append(ToolCapability(tool, ToolState.TIMED_OUT, executable, None, outcome.error or "timed out"))
        elif outcome.state is CommandState.CANCELLED:
            result.append(ToolCapability(tool, ToolState.CANCELLED, executable, None, outcome.error or "cancelled"))
        else:
            message = _limited_text(outcome.stderr) or outcome.error or "capability probe failed"
            result.append(ToolCapability(tool, ToolState.ERROR, executable, None, message))
    return tuple(result)


def capabilities_by_name(capabilities: Sequence[ToolCapability]) -> Dict[ToolName, ToolCapability]:
    result = {capability.tool: capability for capability in capabilities}
    if set(result) != set(ToolName) or len(capabilities) != len(ToolName):
        raise ValueError("capability report must contain every supported tool exactly once")
    return result


def _first_nonempty_line(value: bytes) -> str:
    for line in _decode_text(value).splitlines():
        if line.strip():
            return line.strip()[:MAX_TOOL_TEXT_CHARACTERS]
    return "version output was empty"


def _decode_text(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()


def _limited_text(value: bytes, maximum_characters: int = 2048) -> str:
    text = _decode_text(value)
    if len(text) <= maximum_characters:
        return text
    return text[:maximum_characters] + "\N{HORIZONTAL ELLIPSIS}"


def ffprobe_command(executable: str, path: str) -> Tuple[str, ...]:
    return (
        executable,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        path,
    )


def ffmpeg_scene_command(executable: str, path: str, threshold: float) -> Tuple[str, ...]:
    if not math_is_fraction(threshold):
        raise ValueError("scene threshold must be between 0 and 1")
    return (
        executable,
        "-hide_banner",
        "-nostdin",
        "-i",
        path,
        "-an",
        "-vf",
        "select=gt(scene\\,{:.6f}),showinfo".format(threshold),
        "-vsync",
        "vfr",
        "-f",
        "null",
        "-",
    )


def ffmpeg_frame_command(
    executable: str,
    path: str,
    timestamp_seconds: float,
    *,
    width: int = 32,
    height: int = 32,
) -> Tuple[str, ...]:
    if not math_is_nonnegative_finite(timestamp_seconds):
        raise ValueError("frame timestamp must be finite and non-negative")
    if width <= 0 or height <= 0:
        raise ValueError("frame dimensions must be positive")
    return (
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        "{:.6f}".format(timestamp_seconds),
        "-i",
        path,
        "-frames:v",
        "1",
        "-vf",
        "scale={}:{}:flags=area,format=gray".format(width, height),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    )


def fpcalc_command(executable: str, path: str, *, maximum_seconds: int) -> Tuple[str, ...]:
    if maximum_seconds <= 0:
        raise ValueError("maximum fpcalc duration must be positive")
    return (executable, "-raw", "-json", "-length", str(maximum_seconds), path)


_PTS_TIME = re.compile(r"\bpts_time:([0-9]+(?:\.[0-9]+)?)")


def parse_scene_times(stderr: bytes, duration_seconds: float) -> Tuple[float, ...]:
    if not math_is_nonnegative_finite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("duration must be positive")
    result = set()
    for match in _PTS_TIME.finditer(_decode_text(stderr)):
        timestamp = float(match.group(1))
        if 0 <= timestamp <= duration_seconds:
            result.add(timestamp)
    return tuple(sorted(result))


def parse_ffprobe_json(payload: bytes | str) -> VideoMetadata:
    """Parse the stable subset of ffprobe JSON needed for candidate generation."""

    try:
        document = strict_bounded_json_loads(
            payload,
            max_bytes=MAX_FFPROBE_JSON_BYTES,
            limits=FFPROBE_JSON_LIMITS,
            label="ffprobe JSON",
        )
    except ValueError as error:
        raise ValueError("ffprobe returned invalid JSON") from error
    if not isinstance(document, dict):
        raise ValueError("ffprobe document must be an object")
    streams = document.get("streams")
    file_format = document.get("format", {})
    if not isinstance(streams, list) or not isinstance(file_format, dict):
        raise ValueError("ffprobe document lacks stream or format metadata")
    video = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"),
        None,
    )
    if not isinstance(video, dict):
        raise ValueError("ffprobe document contains no video stream")

    duration = _first_float(video.get("duration"), file_format.get("duration"))
    width = _positive_int(video.get("width"), "video width")
    height = _positive_int(video.get("height"), "video height")
    frame_rate = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    if frame_rate is None or frame_rate <= 0:
        raise ValueError("ffprobe document contains no valid frame rate")
    video_codec = _bounded_metadata_text(video.get("codec_name"), "video codec", allow_empty=False)
    if not video_codec:
        raise ValueError("ffprobe document contains no video codec")

    audio_duration = None
    audio_codec = ""
    if isinstance(audio, dict):
        audio_codec = _bounded_metadata_text(audio.get("codec_name"), "audio codec")
        audio_duration = _optional_float(audio.get("duration"))
    bit_rate = _optional_int(file_format.get("bit_rate"))
    return VideoMetadata(
        duration_seconds=duration,
        width=width,
        height=height,
        frame_rate=frame_rate,
        video_codec=video_codec,
        pixel_format=_bounded_metadata_text(video.get("pix_fmt"), "pixel format"),
        audio_codec=audio_codec,
        audio_duration_seconds=audio_duration,
        bit_rate=bit_rate,
        container=_bounded_metadata_text(file_format.get("format_name"), "container"),
    )


def _first_float(*values: object) -> float:
    for value in values:
        parsed = _optional_float(value)
        if parsed is not None and parsed > 0:
            return parsed
    raise ValueError("ffprobe document contains no positive duration")


def _optional_float(value: object) -> Optional[float]:
    if value in (None, "", "N/A"):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (OverflowError, TypeError, ValueError):
        return None
    return result if math_is_nonnegative_finite(result) else None


def _positive_int(value: object, name: str) -> int:
    result = _optional_int(value)
    if result is None or result <= 0:
        raise ValueError("{} must be positive".format(name))
    return result


def _optional_int(value: object) -> Optional[int]:
    if value in (None, "", "N/A"):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if 0 <= result <= MAX_FFPROBE_INTEGER else None


def _parse_rate(value: object) -> Optional[float]:
    if value in (None, "", "N/A"):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    text = str(value)
    if "/" in text:
        numerator_text, denominator_text = text.split("/", 1)
        try:
            numerator = float(numerator_text)
            denominator = float(denominator_text)
        except ValueError:
            return None
        if denominator == 0:
            return None
        rate = numerator / denominator
    else:
        try:
            rate = float(text)
        except ValueError:
            return None
    return rate if math_is_nonnegative_finite(rate) else None


def _bounded_metadata_text(value, name, *, allow_empty=True):
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str):
        raise ValueError("ffprobe {} must be text".format(name))
    result = value.strip()
    if (not allow_empty and not result) or len(result) > MAX_TOOL_TEXT_CHARACTERS or "\0" in result:
        raise ValueError("ffprobe {} exceeds the supported text limit".format(name))
    return result


def math_is_nonnegative_finite(value: float) -> bool:
    return value >= 0 and value != float("inf") and value == value


def math_is_fraction(value: float) -> bool:
    return math_is_nonnegative_finite(value) and 0 < value < 1


def resolve_source_snapshot(path: str | Path) -> Tuple[str, int, int, bytes]:
    from core.file_generation import get_file_generation_token
    from core.file_identity import get_file_identity

    source = Path(path)
    if source.is_symlink():
        raise ValueError("video source symlinks are not analyzed")
    stat = source.stat(follow_symlinks=False)
    if not source.is_file():
        raise ValueError("video source must be a regular file")
    identity = get_file_identity(source, follow_symlinks=False, stat_result=stat)
    generation = get_file_generation_token(
        source,
        stat_result=stat,
        expected_identity=identity,
    )
    return str(source.resolve(strict=True)), stat.st_size, stat.st_mtime_ns, generation.encoded
