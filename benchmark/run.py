#!/usr/bin/env python3
"""Run deterministic RSS benchmarks against a compiled MicroCodex binary."""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import math
import os
import platform
import pty
import resource
import signal
import statistics
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from server import BenchmarkApi, MODEL


SCENARIOS = ("large-turn", "long-transcript", "reset", "resize", "compaction")


@dataclass
class Sample:
    elapsed_seconds: float
    rss_kib: int
    pss_kib: int | None
    phase: str


@dataclass
class Mark:
    elapsed_seconds: float
    phase: str


def read_process_memory(pid: int) -> tuple[int, int | None] | None:
    """Return RSS and optional PSS in KiB for one process."""
    if sys.platform.startswith("linux"):
        try:
            status = Path(f"/proc/{pid}/status").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return None
        rss = None
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
                break
        if rss is None or rss <= 0:
            return None
        pss = None
        try:
            rollup = Path(f"/proc/{pid}/smaps_rollup").read_text()
            for line in rollup.splitlines():
                if line.startswith("Pss:"):
                    pss = int(line.split()[1])
                    break
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
        return rss, pss

    result = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    text = result.stdout.strip()
    if result.returncode != 0 or not text:
        return None
    rss = int(text.split()[0])
    return (rss, None) if rss > 0 else None


class MeasuredProcess:
    """Run MicroCodex in a PTY while sampling its resident memory."""

    def __init__(
        self,
        binary: Path,
        cwd: Path,
        environment: dict[str, str],
        sample_interval: float,
    ) -> None:
        self.binary = binary
        self.cwd = cwd
        self.environment = environment
        self.sample_interval = sample_interval
        self.samples: list[Sample] = []
        self.marks: list[Mark] = []
        self.phase = "startup"
        self.pid = -1
        self.fd = -1
        self.status: int | None = None
        self.usage: resource.struct_rusage | None = None
        self._started = 0.0
        self._stop = threading.Event()
        self._phase_lock = threading.Lock()
        self._tail = bytearray()
        self._drain_thread: threading.Thread | None = None
        self._sample_thread: threading.Thread | None = None

    def start(self, rows: int = 30, columns: int = 100) -> None:
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.cwd)
            argv = [str(self.binary), "--model", MODEL]
            os.execve(str(self.binary), argv, self.environment)
        self.pid = pid
        self.fd = fd
        self._started = time.monotonic()
        self.resize(rows, columns)
        self.mark("startup")
        self._drain_thread = threading.Thread(target=self._drain_output, daemon=True)
        self._sample_thread = threading.Thread(target=self._sample_memory, daemon=True)
        self._drain_thread.start()
        self._sample_thread.start()

    def mark(self, phase: str) -> None:
        with self._phase_lock:
            self.phase = phase
            self.marks.append(Mark(time.monotonic() - self._started, phase))

    def send_prompt(self, prompt: str) -> None:
        os.write(self.fd, prompt.encode() + b"\r")

    def send_key(self, key: bytes) -> None:
        os.write(self.fd, key)

    def resize(self, rows: int, columns: int) -> None:
        size = struct.pack("HHHH", rows, columns, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, size)

    def stop(self, timeout: float = 5) -> None:
        self.mark("shutdown")
        try:
            self.send_key(b"\x11")  # Ctrl-Q
        except OSError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            waited = os.wait4(self.pid, os.WNOHANG)
            if waited[0] != 0:
                self._record_wait(waited)
                break
            time.sleep(0.05)
        if self.status is None:
            os.kill(self.pid, signal.SIGTERM)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                waited = os.wait4(self.pid, os.WNOHANG)
                if waited[0] != 0:
                    self._record_wait(waited)
                    break
                time.sleep(0.05)
        if self.status is None:
            os.kill(self.pid, signal.SIGKILL)
            self._record_wait(os.wait4(self.pid, 0))
        self._stop.set()
        try:
            os.close(self.fd)
        except OSError:
            pass
        if self._sample_thread is not None:
            self._sample_thread.join(timeout=2)
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=2)

    def terminal_tail(self) -> bytes:
        return bytes(self._tail)

    def _record_wait(self, waited: tuple[int, int, resource.struct_rusage]) -> None:
        _, status, usage = waited
        self.status = os.waitstatus_to_exitcode(status)
        self.usage = usage

    def _drain_output(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = os.read(self.fd, 65_536)
                if not chunk:
                    return
                self._tail.extend(chunk)
                if len(self._tail) > 65_536:
                    del self._tail[:-65_536]
            except OSError:
                return

    def _sample_memory(self) -> None:
        while not self._stop.is_set():
            measured = read_process_memory(self.pid)
            if measured is None:
                return
            with self._phase_lock:
                phase = self.phase
            rss, pss = measured
            self.samples.append(
                Sample(time.monotonic() - self._started, rss, pss, phase)
            )
            self._stop.wait(self.sample_interval)


def write_credentials(codex_home: Path) -> None:
    codex_home.mkdir(parents=True)
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": "benchmark-id-token",
            "access_token": "benchmark-access-token",
            "refresh_token": "benchmark-refresh-token",
            "account_id": "benchmark-account",
        },
    }
    path = codex_home / "auth.json"
    path.write_text(json.dumps(auth))
    path.chmod(0o600)


def percentile(values: list[int], percent: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percent * len(ordered)) - 1)
    return float(ordered[index])


def post_turn_slope(samples: list[Sample]) -> float | None:
    points: list[tuple[int, float]] = []
    phases = sorted(
        {sample.phase for sample in samples if sample.phase.startswith("post_turn_")},
        key=lambda phase: int(phase.rsplit("_", 1)[1]),
    )
    for phase in phases:
        rss = [sample.rss_kib for sample in samples if sample.phase == phase]
        if rss:
            points.append((int(phase.rsplit("_", 1)[1]), statistics.median(rss)))
    if len(points) < 2:
        return None
    mean_x = statistics.mean(point[0] for point in points)
    mean_y = statistics.mean(point[1] for point in points)
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator


def summarize(
    scenario: str,
    run_number: int,
    process: MeasuredProcess,
    final_phase: str,
    wall_seconds: float,
    summaries_completed: int,
) -> dict[str, object]:
    if not process.samples:
        raise RuntimeError("process exited before any memory samples were collected")
    rss = [sample.rss_kib for sample in process.samples]
    active = [sample.rss_kib for sample in process.samples if sample.phase.startswith("active_")]
    post = [sample.rss_kib for sample in process.samples if sample.phase.startswith("post_")]
    final = [sample.rss_kib for sample in process.samples if sample.phase == final_phase]
    usage = process.usage
    max_rss = usage.ru_maxrss if usage else 0
    if sys.platform == "darwin":
        max_rss //= 1024  # Darwin reports bytes; Linux reports KiB.
    return {
        "scenario": scenario,
        "run": run_number,
        "samples": len(rss),
        "average_rss_kib": round(statistics.mean(rss), 2),
        "median_rss_kib": round(statistics.median(rss), 2),
        "p95_rss_kib": round(percentile(rss, 0.95), 2),
        "peak_rss_kib": max(rss),
        "reported_max_rss_kib": max_rss,
        "active_average_rss_kib": round(statistics.mean(active), 2) if active else None,
        "post_average_rss_kib": round(statistics.mean(post), 2) if post else None,
        "final_median_rss_kib": round(statistics.median(final), 2) if final else None,
        "post_turn_slope_kib": round(post_turn_slope(process.samples), 2)
        if post_turn_slope(process.samples) is not None
        else None,
        "wall_seconds": round(wall_seconds, 3),
        "user_cpu_seconds": round(usage.ru_utime, 3) if usage else None,
        "system_cpu_seconds": round(usage.ru_stime, 3) if usage else None,
        "compactions": summaries_completed,
        "exit_status": process.status,
    }


def run_turn(
    process: MeasuredProcess,
    api: BenchmarkApi,
    turn: int,
    settle_seconds: float,
) -> None:
    process.mark(f"active_turn_{turn}")
    process.send_prompt(f"benchmark turn {turn}")
    api.wait_for_turns(turn)
    time.sleep(0.15)  # Let the UI collect its completed worker future.
    process.mark(f"post_turn_{turn}")
    time.sleep(settle_seconds)


def scenario_large_turn(
    process: MeasuredProcess, api: BenchmarkApi, args: argparse.Namespace
) -> str:
    run_turn(process, api, 1, args.settle_seconds * 2)
    return "post_turn_1"


def scenario_long_transcript(
    process: MeasuredProcess, api: BenchmarkApi, args: argparse.Namespace
) -> str:
    for turn in range(1, args.turns + 1):
        run_turn(process, api, turn, args.settle_seconds)
    return f"post_turn_{args.turns}"


def scenario_reset(
    process: MeasuredProcess, api: BenchmarkApi, args: argparse.Namespace
) -> str:
    turns = max(6, args.turns // 2)
    for turn in range(1, turns + 1):
        run_turn(process, api, turn, args.settle_seconds / 2)
    process.mark("before_reset")
    time.sleep(args.settle_seconds)
    process.send_key(b"\x12")  # Ctrl-R
    process.mark("post_reset")
    time.sleep(args.settle_seconds * 2)
    return "post_reset"


def scenario_resize(
    process: MeasuredProcess, api: BenchmarkApi, args: argparse.Namespace
) -> str:
    turns = max(5, args.turns // 3)
    for turn in range(1, turns + 1):
        run_turn(process, api, turn, args.settle_seconds / 2)
    process.mark("before_resize")
    time.sleep(args.settle_seconds)
    for index, columns in enumerate((40, 120, 40, 120, 40, 100), start=1):
        process.mark(f"active_resize_{index}")
        process.resize(30, columns)
        time.sleep(args.settle_seconds / 2)
    process.mark("post_resize")
    time.sleep(args.settle_seconds * 2)
    return "post_resize"


def scenario_compaction(
    process: MeasuredProcess, api: BenchmarkApi, args: argparse.Namespace
) -> str:
    turns = max(8, args.turns // 2)
    for turn in range(1, turns + 1):
        run_turn(process, api, turn, args.settle_seconds / 2)
    if api.summaries_completed == 0:
        raise RuntimeError("compaction scenario completed without a summary request")
    return f"post_turn_{turns}"


SCENARIO_RUNNERS: dict[
    str, Callable[[MeasuredProcess, BenchmarkApi, argparse.Namespace], str]
] = {
    "large-turn": scenario_large_turn,
    "long-transcript": scenario_long_transcript,
    "reset": scenario_reset,
    "resize": scenario_resize,
    "compaction": scenario_compaction,
}


def write_timeline(path: Path, samples: list[Sample]) -> None:
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=asdict(samples[0]).keys())
        writer.writeheader()
        writer.writerows(asdict(sample) for sample in samples)


def write_marks(path: Path, marks: list[Mark]) -> None:
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=("elapsed_seconds", "phase"))
        writer.writeheader()
        writer.writerows(asdict(mark) for mark in marks)


def benchmark_once(
    binary: Path,
    scenario: str,
    run_number: int,
    output: Path,
    args: argparse.Namespace,
) -> dict[str, object]:
    response_bytes = args.response_kib * 1024
    api = BenchmarkApi(response_bytes=response_bytes)
    api.start()
    process: MeasuredProcess | None = None
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="microcodex-benchmark-") as temporary:
            root = Path(temporary)
            codex_home = root / "codex-home"
            project = root / "project"
            project.mkdir()
            write_credentials(codex_home)
            environment = os.environ.copy()
            environment.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "HOME": str(root),
                    "TERM": "xterm-256color",
                    "MICROCODEX_API_ENDPOINT": api.endpoint,
                    "MICROCODEX_RETAINED_CONTEXT_TOKENS": "1000",
                    "MICROCODEX_COMPACT_AT_TOKENS": "5000"
                    if scenario == "compaction"
                    else "100000000",
                }
            )
            process = MeasuredProcess(
                binary, project, environment, args.sample_ms / 1000
            )
            process.start()
            api.wait_for_models()
            time.sleep(0.3)
            final_phase = SCENARIO_RUNNERS[scenario](process, api, args)
            process.stop()
            if process.status != 0:
                tail_path = output / f"{scenario}-run-{run_number}-terminal-tail.bin"
                tail_path.write_bytes(process.terminal_tail())
                raise RuntimeError(
                    f"MicroCodex exited with {process.status}; terminal tail: {tail_path}"
                )
            timeline = output / f"{scenario}-run-{run_number}-timeline.csv"
            marks = output / f"{scenario}-run-{run_number}-marks.csv"
            write_timeline(timeline, process.samples)
            write_marks(marks, process.marks)
            return summarize(
                scenario,
                run_number,
                process,
                final_phase,
                time.monotonic() - started,
                api.summaries_completed,
            )
    finally:
        if process is not None and process.status is None:
            process.stop()
        api.close()


def write_summary(output: Path, rows: list[dict[str, object]], metadata: dict[str, object]) -> None:
    with (output / "summary.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    (output / "summary.json").write_text(
        json.dumps({"metadata": metadata, "runs": rows}, indent=2) + "\n"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark RSS for a compiled MicroCodex binary."
    )
    parser.add_argument("binary", type=Path, help="path to the compiled MicroCodex binary")
    parser.add_argument("--runs", type=int, default=3, help="fresh processes per scenario")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=SCENARIOS,
        dest="scenarios",
        help="scenario to run; may be repeated (default: all)",
    )
    parser.add_argument("--turns", type=int, default=18, help="turns in the long scenario")
    parser.add_argument(
        "--response-kib", type=int, default=20, help="assistant Markdown bytes per turn"
    )
    parser.add_argument("--sample-ms", type=int, default=100, help="RSS sampling interval")
    parser.add_argument(
        "--settle-seconds", type=float, default=0.75, help="quiet sampling after events"
    )
    parser.add_argument("--label", help="result label; defaults to the binary filename")
    parser.add_argument("--output", type=Path, help="result directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    binary = args.binary.expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise SystemExit(f"not an executable file: {binary}")
    if args.runs < 1 or args.turns < 1 or args.response_kib < 1 or args.sample_ms < 10:
        raise SystemExit("runs, turns, and response size must be positive; sample-ms must be >= 10")
    scenarios = args.scenarios or list(SCENARIOS)
    label = args.label or binary.name
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    output = (args.output or Path(__file__).parent / "results" / f"{label}-{timestamp}").resolve()
    output.mkdir(parents=True, exist_ok=False)

    metadata: dict[str, object] = {
        "binary": str(binary),
        "label": label,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sample_ms": args.sample_ms,
        "response_kib": args.response_kib,
        "turns": args.turns,
        "settle_seconds": args.settle_seconds,
        "scenarios": scenarios,
        "runs_per_scenario": args.runs,
    }
    rows: list[dict[str, object]] = []
    total = len(scenarios) * args.runs
    completed = 0
    for scenario in scenarios:
        for run_number in range(1, args.runs + 1):
            completed += 1
            print(f"[{completed}/{total}] {scenario} run {run_number}", flush=True)
            row = benchmark_once(binary, scenario, run_number, output, args)
            rows.append(row)
            print(
                f"  average={row['average_rss_kib']} KiB "
                f"peak={row['peak_rss_kib']} KiB "
                f"final={row['final_median_rss_kib']} KiB",
                flush=True,
            )
    write_summary(output, rows, metadata)
    print(f"results: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
