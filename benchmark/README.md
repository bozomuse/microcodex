# MicroCodex memory benchmark

This harness measures a compiled MicroCodex binary under deterministic terminal workloads. It launches every run as a fresh process in a real PTY, serves fixed Markdown responses from a loopback Codex API, and samples the process's RSS throughout each phase.

The binary is always an explicit positional argument. This keeps builds out of the harness and makes it possible to benchmark a branch build and an already-installed native binary with identical inputs.

## Quick start

Run the current branch build:

```sh
python3 benchmark/run.py ./build/microcodex --label candidate
```

Run an installed compiled binary:

```sh
python3 benchmark/run.py /absolute/path/to/microcodex-native --label baseline
```

The default suite runs each scenario three times. For a faster smoke test:

```sh
python3 benchmark/run.py ./build/microcodex \
  --runs 1 \
  --turns 8 \
  --scenario large-turn \
  --scenario long-transcript \
  --output /tmp/microcodex-smoke
```

Compare two completed suites:

```sh
python3 benchmark/compare.py \
  benchmark/results/baseline-YYYYMMDD-HHMMSS \
  benchmark/results/candidate-YYYYMMDD-HHMMSS
```

The comparison reports the median of each metric across runs and the percentage change from baseline to candidate. For memory and time metrics, a negative change is generally an improvement. It rejects result sets with different response sizes, turn counts, sampling intervals, or settling windows.

## Scenarios

- `large-turn`: streams one large, richly formatted Markdown response and measures the quiet post-turn plateau.
- `long-transcript`: accumulates assistant turns near the UI transcript limit and records the post-turn RSS slope.
- `reset`: fills the transcript, records a pre-reset plateau, sends Ctrl-R, and measures memory after the transcript and caches are released.
- `resize`: fills the transcript and repeatedly alternates between narrow and wide PTY sizes, forcing Markdown layout invalidation.
- `compaction`: lowers the context thresholds, performs repeated turns, verifies that at least one summary request occurred, and measures memory around context replacement.

Normal scenarios set the compaction threshold high so compaction does not interfere with cache measurements. The compaction scenario sets a low threshold and fails if no summary request is observed.

## Output

Each result directory contains:

- `summary.json`: run metadata and machine-readable metrics.
- `summary.csv`: one aggregate row per scenario run.
- `<scenario>-run-<n>-timeline.csv`: timestamped RSS/PSS samples with phase labels.
- `<scenario>-run-<n>-marks.csv`: exact phase transition times.
- A bounded terminal-output tail only when MicroCodex exits unsuccessfully.

Important summary metrics include:

- `average_rss_kib`: average of all periodic RSS samples.
- `peak_rss_kib`: highest periodic RSS sample.
- `reported_max_rss_kib`: operating-system high-water value from process resource usage.
- `active_average_rss_kib`: average while turns or resizes are active.
- `post_average_rss_kib`: average across post-event quiet windows.
- `final_median_rss_kib`: median RSS in the scenario's final quiet window.
- `post_turn_slope_kib`: linear RSS growth per turn, using each post-turn median.
- CPU and wall-clock time, which expose memory-versus-recomputation tradeoffs.
- `compactions`: number of summary responses served during the run.

On Linux, the timeline also records PSS from `/proc/<pid>/smaps_rollup` when permitted. On macOS, RSS is sampled with `ps`. The benchmark measures the direct process identified by the provided executable path, so pass the native compiled binary rather than a launcher that starts another process.

## Useful options

```text
--runs N                 fresh processes per scenario (default: 3)
--scenario NAME          select a scenario; repeat to select several
--turns N                turns in the long transcript scenario (default: 18)
--response-kib N         Markdown response size per turn (default: 20)
--sample-ms N            RSS sampling interval (default: 100)
--settle-seconds N       quiet measurement period after events (default: 0.75)
--label NAME             label saved in metadata and used by comparisons
--output DIRECTORY       explicit new result directory
```

The mock response must remain below MicroCodex's 64 KiB HTTP response limit. The default 20 KiB text appears twice in the SSE payload—once as streamed deltas and once in the completed message item—and safely stays below that transport limit.

## Producing trustworthy comparisons

Use the same options, terminal environment, and machine for both binaries. Close memory-heavy background applications and run the suites close together. More repetitions improve confidence:

```sh
python3 benchmark/run.py /path/to/baseline --label baseline --runs 10
python3 benchmark/run.py /path/to/candidate --label candidate --runs 10
```

Inspect the timelines as well as the aggregate table. Average RSS can hide a high temporary peak or a poor post-turn plateau. The allocator and cache changes are expected to improve post-turn, reset, compaction, and long-transcript retention; temporary render peaks may remain.

The harness intentionally uses only the Python standard library and does not access production credentials or the production API. Every run receives an isolated temporary home, credentials file, project directory, and conversation store.
