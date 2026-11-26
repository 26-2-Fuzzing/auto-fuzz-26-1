#!/usr/bin/env python3
"""Offline anomaly analysis for CANoe2 paired fuzzing campaigns.

The CANoe2 ``plan.json`` schedule is authoritative. Schema-v2 campaigns may
contain 20 frames at 50 ms or 10 frames at 100 ms and multiple temporal
payloads. Older schema-v1 static captures are accepted by inferring a schedule
from their recorded slot count and mutation payload.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import math
import re
import statistics
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Counter, DefaultDict, Iterable, Iterator, Mapping, Sequence


REPOSITORY_URL = "https://github.com/26-2-Fuzzing/auto-fuzz-26-1"
REPOSITORY_BRANCH = "CANoe2"
CAPTURE_CONTRACT_FILES = (
    "pi_can_lab/canoe_paired_cycle.py",
    "pi_can_lab/canoe_paired_validation.py",
    "pi_can_lab/canoe_schedule.py",
    "pi_can_lab/canoe/capl/FuzzHarness.can",
)
PHASE_ORDER = ("baseline", "normal", "slot", "recovery")
WINDOWS = ("baseline5", "normal1", "normal5", "slot1", "recovery_early5", "recovery_late5")
COMPARE_WINDOWS = ("slot1", "recovery_early5", "recovery_late5")
WINDOW_SECONDS = {"slot1": 1.0, "recovery_early5": 5.0, "recovery_late5": 5.0}
SEVERITY_RANK = {"NORMAL": 0, "WARNING": 1, "CANDIDATE": 2, "STRONG": 3}
CRITICAL_WORDS = ("crash", "airbag", "rollover", "ecall", "panic", "panik", "emergency", "notbrems", "fehler", "fault", "dwa_alarm")
PROTECTED_WORDS = ("checksum", "crc", "counter", "cnt", "alive", "rolling", "botschaftszaehler")
ASC_FRAME_RE = re.compile(rb"^\s*(\d+(?:\.\d+)?)\s+(\d+)\s+([0-9A-Fa-f]+)(x?)\s+(Rx|Tx)\s+d\s+(\d+)\s+(.*)$")
ASC_BASE_RE = re.compile(rb"^\s*base\s+(dec|hex)\s+timestamps\s+absolute\s*$", re.I)
HEALTH_RE = re.compile(rb"::FuzzIO::(BusOff|ErrorFrame)([123])\s*=\s*L?(-?\d+)\b")
CASE_RE = re.compile(r"(?:^|/)case_(\d{4})(?:/|$)")

FrameKey = tuple[int, int, bool]
SignalKey = tuple[int, int, str]
ScheduleFrame = tuple[int, bytes]


def repository_revision() -> dict[str, Any]:
    """Return the analyzer checkout revision without asserting log provenance."""
    candidate = Path(__file__).resolve().parent.parent

    def run_git(*arguments: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "-C", str(candidate), *arguments],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        value = completed.stdout.strip()
        return value or None

    root = run_git("rev-parse", "--show-toplevel")
    if root is None:
        return {"root": None, "branch": None, "commit_sha": None, "tree_sha": None}
    return {
        "root": root,
        "branch": run_git("branch", "--show-current"),
        "commit_sha": run_git("rev-parse", "HEAD"),
        "tree_sha": run_git("rev-parse", "HEAD^{tree}"),
    }


def parse_int(value: str) -> int:
    return int(value, 0)


def percentile(values: Iterable[float], fraction: float) -> float | None:
    data = sorted(values)
    if not data:
        return None
    position = (len(data) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(data) - 1)
    return data[low] * (high - position) + data[high] * (position - low)


def median(values: Iterable[float]) -> float | None:
    data = list(values)
    return statistics.median(data) if data else None


def highest_severity(values: Iterable[str]) -> str:
    return max(values, key=lambda item: SEVERITY_RANK[item], default="NORMAL")


def frame_key_text(key: FrameKey) -> str:
    channel, can_id, extended = key
    return f"CAN{channel}:0x{can_id:X}{'x' if extended else ''}"


def signal_key_text(key: SignalKey) -> str:
    channel, can_id, name = key
    return f"CAN{channel}:0x{can_id:X}:{name}"


def distance_from_range(value: int, low: int, high: int) -> int:
    return low - value if value < low else value - high if value > high else 0


def normalized(counter: Mapping[Any, int]) -> dict[Any, float]:
    total = sum(counter.values())
    return {key: value / total for key, value in counter.items()} if total else {}


def total_variation(left: Mapping[Any, int], right: Mapping[Any, int]) -> float:
    p, q = normalized(left), normalized(right)
    return 0.5 * sum(abs(p.get(key, 0.0) - q.get(key, 0.0)) for key in set(p) | set(q))


def dominant(counter: Mapping[Any, int]) -> tuple[Any | None, float]:
    total = sum(counter.values())
    if not total:
        return None, 0.0
    value = max(counter, key=counter.get)  # type: ignore[arg-type]
    return value, counter[value] / total


def is_protected_signal(name: str) -> bool:
    lowered = name.lower().replace("_", "")
    return (
        any(word.replace("_", "") in lowered for word in PROTECTED_WORDS)
        or re.search(r"(?:^|_)bz(?:_|$)", name, re.I) is not None
    )


class CampaignSource:
    def __init__(self, source: Path):
        self.source = source.resolve()
        self._zip: zipfile.ZipFile | None = None
        if self.source.is_file():
            self._zip = zipfile.ZipFile(self.source)
            self._names = [name for name in self._zip.namelist() if not name.endswith("/")]
        elif self.source.is_dir():
            self._names = [path.relative_to(self.source).as_posix() for path in self.source.rglob("*") if path.is_file()]
        else:
            raise FileNotFoundError(f"Campaign source not found: {self.source}")

    def __enter__(self) -> "CampaignSource":
        return self

    def __exit__(self, *_: object) -> None:
        if self._zip is not None:
            self._zip.close()

    @property
    def names(self) -> list[str]:
        return self._names

    def matches(self, predicate: Callable[[str], bool]) -> list[str]:
        return [name for name in self._names if predicate(name)]

    def one(self, predicate: Callable[[str], bool], description: str) -> str:
        matches = self.matches(predicate)
        if len(matches) != 1:
            raise ValueError(f"Expected one {description}, found {len(matches)}: {matches[:5]}")
        return matches[0]

    def read_bytes(self, name: str) -> bytes:
        if self._zip is not None:
            return self._zip.read(name)
        return (self.source / PurePosixPath(name)).read_bytes()

    def read_json(self, name: str) -> dict[str, Any]:
        value = json.loads(self.read_bytes(name).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"Expected JSON object: {name}")
        return value

    @contextlib.contextmanager
    def open_binary(self, name: str) -> Iterator[BinaryIO]:
        if self._zip is not None:
            with self._zip.open(name) as stream:
                yield stream
        else:
            with (self.source / PurePosixPath(name)).open("rb") as stream:
                yield stream

    def sha256(self, name: str) -> str:
        digest = hashlib.sha256()
        with self.open_binary(name) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


class SignalDecoder:
    kind = "none"

    def __init__(self) -> None:
        self.sources: list[str] = []
        self.warnings: list[str] = []

    def decode(self, can_id: int, payload: bytes) -> dict[str, Any]:
        return {}

    def profile(self, can_id: int, name: str) -> dict[str, Any]:
        return {}


class AuditSignalDecoder(SignalDecoder):
    kind = "audit_json"

    def __init__(self, paths: Iterable[Path]):
        super().__init__()
        self.fields: dict[int, list[tuple[str, int, int]]] = {}
        self.profiles: dict[tuple[int, str], dict[str, Any]] = {}
        for path in paths:
            resolved = path.resolve(strict=True)
            content = json.loads(resolved.read_text(encoding="utf-8"))
            for raw_id, definitions in content.get("fields", {}).items():
                can_id = int(raw_id, 0)
                parsed = [(str(name), int(start), int(length)) for name, start, length in definitions]
                if can_id in self.fields and self.fields[can_id] != parsed:
                    raise ValueError(f"Conflicting audit definitions for 0x{can_id:X}")
                self.fields[can_id] = parsed
                for name, _, length in parsed:
                    self.profiles[(can_id, name)] = {
                        "bit_length": length, "special_values": set(), "scale": None,
                    }
            self.sources.append(str(resolved))
        self.warnings.append("Audit JSON is a linear little-endian raw-bit fallback; scaling and multiplexing are unavailable.")

    def decode(self, can_id: int, payload: bytes) -> dict[str, Any]:
        raw = int.from_bytes(payload, "little")
        result: dict[str, Any] = {}
        for name, start, length in self.fields.get(can_id, []):
            if length > 0 and start >= 0 and start + length <= len(payload) * 8:
                result[name] = (raw >> start) & ((1 << length) - 1)
        return result

    def profile(self, can_id: int, name: str) -> dict[str, Any]:
        return self.profiles.get((can_id, name), {})


class CantoolsSignalDecoder(SignalDecoder):
    kind = "dbc_cantools"

    def __init__(self, path: Path, expected_sha256: str | None):
        super().__init__()
        resolved = path.resolve(strict=True)
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if expected_sha256 and digest.lower() != expected_sha256.lower():
            raise ValueError(f"DBC SHA-256 mismatch: campaign={expected_sha256}, file={digest}")
        try:
            import cantools  # type: ignore
        except ImportError as exc:
            raise RuntimeError("--dbc requires cantools from requirements-canoe.txt") from exc
        self.database = cantools.database.load_file(str(resolved))
        self.profiles: dict[tuple[int, str], dict[str, Any]] = {}
        for message in self.database.messages:
            for signal in message.signals:
                self.profiles[(message.frame_id, signal.name)] = {
                    "bit_length": int(signal.length),
                    "special_values": set((signal.choices or {}).keys()),
                    "scale": float(signal.scale),
                }
        self.sources = [str(resolved)]

    def decode(self, can_id: int, payload: bytes) -> dict[str, Any]:
        try:
            message = self.database.get_message_by_frame_id(can_id)
            values = message.decode(payload, decode_choices=False, scaling=False, allow_truncated=False)
        except Exception:
            return {}
        return {str(name): value for name, value in values.items() if isinstance(value, (int, float, str))}

    def profile(self, can_id: int, name: str) -> dict[str, Any]:
        return self.profiles.get((can_id, name), {})


@dataclasses.dataclass
class Finding:
    finding_id: str
    detector: str
    severity: str
    confidence: str
    case_number: int
    window: str
    subject: str
    channels: list[int]
    detail: str
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        result = dataclasses.asdict(self)
        # Duplicate the most useful evidence keys at the top level so that the
        # JSON stays pleasant to filter from jq, pandas, and Notion examples.
        if "can_id" in self.evidence:
            result["can_id"] = f"0x{int(self.evidence['can_id']):X}"
        if "extended" in self.evidence:
            result["extended"] = bool(self.evidence["extended"])
        if "signal" in self.evidence:
            result["signal"] = self.evidence["signal"]
        return result


@dataclasses.dataclass
class EpisodeStats:
    case_number: int
    role: str
    asc_name: str
    phases: dict[str, dict[str, float]]
    windows: dict[str, tuple[float, float]]
    asc_base: str | None = None
    header_valid: bool = False
    frame_like_lines: int = 0
    malformed_frames: int = 0
    total_rx_frames: int = 0
    mapped_rx_frames: int = 0
    health_seen: set[tuple[str, int]] = dataclasses.field(default_factory=set)
    health_nonzero: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    pre_ids: set[FrameKey] = dataclasses.field(default_factory=set)
    rx_counts: DefaultDict[str, Counter[FrameKey]] = dataclasses.field(default_factory=lambda: collections.defaultdict(collections.Counter))
    rx_totals: DefaultDict[str, Counter[int]] = dataclasses.field(default_factory=lambda: collections.defaultdict(collections.Counter))
    payload_counts: DefaultDict[str, DefaultDict[FrameKey, Counter[str]]] = dataclasses.field(default_factory=lambda: collections.defaultdict(lambda: collections.defaultdict(collections.Counter)))
    signal_counts: DefaultDict[str, DefaultDict[SignalKey, Counter[Any]]] = dataclasses.field(default_factory=lambda: collections.defaultdict(lambda: collections.defaultdict(collections.Counter)))
    signal_max_runs: DefaultDict[str, DefaultDict[SignalKey, Counter[Any]]] = dataclasses.field(default_factory=lambda: collections.defaultdict(lambda: collections.defaultdict(collections.Counter)))
    signal_transitions: DefaultDict[str, Counter[SignalKey]] = dataclasses.field(default_factory=lambda: collections.defaultdict(collections.Counter))
    gaps_ms: DefaultDict[str, DefaultDict[FrameKey, list[float]]] = dataclasses.field(default_factory=lambda: collections.defaultdict(lambda: collections.defaultdict(list)))
    target_tx: DefaultDict[str, list[tuple[float, str]]] = dataclasses.field(default_factory=lambda: collections.defaultdict(list))
    target_slot_rx: Counter[tuple[int, str]] = dataclasses.field(default_factory=collections.Counter)


def build_windows(phases: Mapping[str, Mapping[str, float]]) -> dict[str, tuple[float, float]]:
    seconds = {label: (float(value["start_ns"]) / 1e9, float(value["end_ns"]) / 1e9) for label, value in phases.items()}
    return {
        "baseline5": (seconds["baseline"][1] - 5.0, seconds["baseline"][1]),
        "normal1": (seconds["normal"][1] - 1.0, seconds["normal"][1]),
        "normal5": (seconds["normal"][1] - 5.0, seconds["normal"][1]),
        "slot1": seconds["slot"],
        "recovery_early5": (seconds["recovery"][0], seconds["recovery"][0] + 5.0),
        "recovery_late5": (seconds["recovery"][1] - 5.0, seconds["recovery"][1]),
    }


def phase_for_time(timestamp: float, phases: Mapping[str, Mapping[str, float]]) -> str | None:
    for label, phase in phases.items():
        if float(phase["start_ns"]) / 1e9 <= timestamp < float(phase["end_ns"]) / 1e9:
            return label
    return None


def window_hits(timestamp: float, windows: Mapping[str, tuple[float, float]]) -> list[str]:
    return [name for name, (start, end) in windows.items() if start <= timestamp < end]


def parse_episode(
    source: CampaignSource,
    asc_name: str,
    case_number: int,
    role: str,
    phases: dict[str, dict[str, float]],
    decoder: SignalDecoder,
    target_id: int,
    source_channel: int,
) -> EpisodeStats:
    windows = build_windows(phases)
    stats = EpisodeStats(case_number, role, asc_name, phases, windows)
    last_time: DefaultDict[str, dict[FrameKey, float]] = collections.defaultdict(dict)
    run_state: DefaultDict[str, dict[SignalKey, tuple[Any, int]]] = collections.defaultdict(dict)
    pre_ranges = [
        (float(phases[label]["start_ns"]) / 1e9, float(phases[label]["end_ns"]) / 1e9)
        for label in ("baseline", "normal")
    ]
    with source.open_binary(asc_name) as stream:
        for line in stream:
            base_match = ASC_BASE_RE.fullmatch(line.strip())
            if base_match:
                stats.asc_base = base_match.group(1).decode("ascii").lower()
                stats.header_valid = True
                continue
            health_match = HEALTH_RE.search(line)
            if health_match:
                kind = health_match.group(1).decode("ascii")
                channel = int(health_match.group(2))
                value = int(health_match.group(3))
                stats.health_seen.add((kind, channel))
                if value != 0:
                    stats.health_nonzero.append({"kind": kind, "channel": channel, "value": value})
            match = ASC_FRAME_RE.match(line)
            if not match:
                if re.match(rb"^\s*\d+(?:\.\d+)?\s+\d+\s+", line):
                    stats.frame_like_lines += 1
                    if b"Rx" in line or b"Tx" in line:
                        stats.malformed_frames += 1
                continue
            stats.frame_like_lines += 1
            if stats.asc_base is None:
                stats.malformed_frames += 1
                continue
            radix = 16 if stats.asc_base == "hex" else 10
            try:
                timestamp = float(match.group(1))
                channel = int(match.group(2))
                can_id = int(match.group(3), radix)
                extended = bool(match.group(4))
                direction = match.group(5).decode("ascii")
                dlc = int(match.group(6), radix)
                tokens = match.group(7).split()
                if not 0 <= dlc <= 8 or len(tokens) < dlc:
                    raise ValueError("invalid DLC")
                payload = bytes(int(token, radix) for token in tokens[:dlc])
            except (ValueError, OverflowError):
                stats.malformed_frames += 1
                continue
            key = (channel, can_id, extended)
            phase = phase_for_time(timestamp, phases)
            if can_id == target_id and not extended and direction == "Tx" and channel == source_channel:
                stats.target_tx[phase or "outside"].append((timestamp, payload.hex().upper()))
            if can_id == target_id and not extended and direction == "Rx" and phase == "slot":
                stats.target_slot_rx[(channel, payload.hex().upper())] += 1
            if direction != "Rx":
                continue
            stats.total_rx_frames += 1
            if any(start <= timestamp < end for start, end in pre_ranges):
                stats.pre_ids.add(key)
            hits = window_hits(timestamp, windows)
            if not hits:
                continue
            values = decoder.decode(can_id, payload) if not extended else {}
            if values:
                stats.mapped_rx_frames += 1
            for window in hits:
                stats.rx_counts[window][key] += 1
                stats.rx_totals[window][channel] += 1
                stats.payload_counts[window][key][payload.hex().upper()] += 1
                previous_time = last_time[window].get(key)
                if previous_time is not None and timestamp > previous_time:
                    stats.gaps_ms[window][key].append((timestamp - previous_time) * 1000.0)
                last_time[window][key] = timestamp
                for name, value in values.items():
                    signal_key = (channel, can_id, name)
                    stats.signal_counts[window][signal_key][value] += 1
                    previous_value, run = run_state[window].get(signal_key, (value, 0))
                    if value == previous_value:
                        run += 1
                    else:
                        stats.signal_transitions[window][signal_key] += 1
                        run = 1
                    run_state[window][signal_key] = (value, run)
                    stats.signal_max_runs[window][signal_key][value] = max(
                        stats.signal_max_runs[window][signal_key][value], run
                    )
    return stats


def scan_background_ids(source: CampaignSource, names: Sequence[str]) -> tuple[set[FrameKey], list[str]]:
    result: set[FrameKey] = set()
    warnings: list[str] = []
    for name in names:
        asc_base: str | None = None
        with source.open_binary(name) as stream:
            for line in stream:
                base_match = ASC_BASE_RE.fullmatch(line.strip())
                if base_match:
                    asc_base = base_match.group(1).decode("ascii").lower()
                    continue
                match = ASC_FRAME_RE.match(line)
                if not match or match.group(5) != b"Rx" or asc_base is None:
                    continue
                radix = 16 if asc_base == "hex" else 10
                try:
                    result.add((int(match.group(2)), int(match.group(3), radix), bool(match.group(4))))
                except ValueError:
                    continue
        if asc_base is None:
            warnings.append(f"reference baseline ASC has no supported base header: {name}")
    return result, warnings


def discover_case_numbers(source: CampaignSource, start: int | None, end: int | None) -> list[int]:
    numbers = sorted({int(match.group(1)) for name in source.names if (match := CASE_RE.search(name))})
    return [number for number in numbers if (start is None or number >= start) and (end is None or number <= end)]


def one_case_json(source: CampaignSource, number: int, filename: str) -> tuple[str | None, dict[str, Any] | None]:
    marker = f"case_{number:04d}/"
    matches = [name for name in source.names if marker in name and name.endswith(f"/{filename}")]
    if len(matches) != 1:
        return None, None
    return matches[0], source.read_json(matches[0])


def parse_schedule(
    plan: Mapping[str, Any], complete: Mapping[str, Any] | None,
) -> tuple[list[ScheduleFrame], bool, list[str]]:
    reasons: list[str] = []
    legacy = False
    baseline_hex = str(plan.get("baseline_payload_hex", "")).replace(" ", "").upper()
    try:
        baseline = bytes.fromhex(baseline_hex)
    except ValueError:
        baseline = b""
    raw = plan.get("mutation_slot_frames")
    schedule: list[ScheduleFrame] = []
    if isinstance(raw, list) and raw:
        try:
            schedule = [
                (int(frame["offset_ms"]), bytes.fromhex(str(frame["payload_hex"])))
                for frame in raw
            ]
        except (KeyError, TypeError, ValueError):
            reasons.append("plan mutation_slot_frames is malformed")
            return [], False, reasons
    else:
        legacy = True
        count = 0
        if complete:
            for wrapper in complete.get("episodes", []):
                validation = (wrapper.get("episode") or {}).get("validation") or {}
                count = max(count, int((validation.get("target_tx_by_phase") or {}).get("slot", 0)))
                phases = (wrapper.get("episode") or {}).get("phases") or []
                for phase in phases:
                    if phase.get("label") == "slot":
                        count = max(count, int(phase.get("submitted_frames", 0)))
        mutation_hex = str(plan.get("mutation_payload_hex", "")).replace(" ", "").upper()
        try:
            mutation = bytes.fromhex(mutation_hex)
        except ValueError:
            mutation = b""
        if count <= 0:
            count = 20
        interval = 1000 // count if count and 1000 % count == 0 else 50
        schedule = [(index * interval, mutation) for index in range(count)]
    if len(baseline) != 8 or any(len(payload) != 8 for _, payload in schedule):
        reasons.append("baseline or slot payload is not eight bytes")
    if len(schedule) not in (10, 20):
        reasons.append(f"slot schedule has {len(schedule)} frames; expected 10 or 20")
    if schedule:
        interval = 1000 // len(schedule)
        if any(offset != index * interval for index, (offset, _) in enumerate(schedule)):
            reasons.append("slot offsets do not cover 1s uniformly")
    if plan.get("slot_frame_count") is not None and int(plan["slot_frame_count"]) != len(schedule):
        reasons.append("slot_frame_count differs from mutation_slot_frames")
    return schedule, legacy, reasons


def locate_episode_asc(source: CampaignSource, number: int, position: int, role: str) -> str:
    marker = f"case_{number:04d}/episode_{position}_{role}/capture/raw/"
    return source.one(lambda name: marker in name and name.lower().endswith(".asc"), f"case {number} {role} ASC")


def validate_case_metadata(
    source: CampaignSource,
    campaign: Mapping[str, Any],
    number: int,
) -> tuple[str, list[str], dict[str, Any] | None]:
    reasons: list[str] = []
    _, plan = one_case_json(source, number, "plan.json")
    _, complete = one_case_json(source, number, "complete.json")
    _, excluded = one_case_json(source, number, "excluded.json")
    _, error = one_case_json(source, number, "error.json")
    if excluded is not None and complete is None:
        return "EXCLUDED", [str(excluded.get("status", "excluded"))], {"plan": plan, "excluded": excluded}
    if complete is None:
        detail = str(error.get("message", "partial or missing complete.json")) if error else "partial or missing complete.json"
        return "INCOMPLETE", [detail], {"plan": plan, "error": error}
    if plan is None:
        return "INVALID", ["missing plan.json"], None
    campaign_schema = int(campaign.get("schema_version", 1))
    plan_schema = int(plan.get("schema_version", campaign_schema))
    if campaign_schema not in (1, 2) or plan_schema not in (1, 2):
        reasons.append("unsupported campaign/plan schema")
    if campaign.get("workflow") != "canoe_paired_capture":
        reasons.append("campaign workflow is not canoe_paired_capture")
    if complete.get("status") != "captured":
        reasons.append("complete status is not captured")
    if complete.get("plan") is not None and complete.get("plan") != plan:
        reasons.append("complete.plan differs from plan.json")
    if int(plan.get("case_number", -1)) != number:
        reasons.append("plan case_number differs from directory")
    campaign_baseline = str(campaign.get("baseline_payload_hex", "")).replace(" ", "").upper()
    plan_baseline = str(plan.get("baseline_payload_hex", "")).replace(" ", "").upper()
    if campaign_baseline and plan_baseline != campaign_baseline:
        reasons.append("plan baseline_payload_hex differs from campaign")
    if plan_schema >= 2 and campaign.get("method") and plan.get("method") != campaign.get("method"):
        reasons.append("plan method differs from campaign")
    for key in ("catalog_sha256", "dbc_sha256"):
        if campaign.get(key) and plan.get(key) != campaign.get(key):
            reasons.append(f"plan {key} differs from campaign")
    order = plan.get("order")
    if order not in (["mutation", "noop"], ["noop", "mutation"]):
        reasons.append("plan order is not mutation/noop pair")
        order = ["mutation", "noop"]
    episodes = complete.get("episodes")
    if not isinstance(episodes, list) or len(episodes) != 2:
        reasons.append("complete must contain two episodes")
        episodes = []
    elif [wrapper.get("role") for wrapper in episodes] != order:
        reasons.append("complete episode order differs from plan.order")
    schedule, legacy, schedule_reasons = parse_schedule(plan, complete)
    reasons.extend(schedule_reasons)
    phase_seconds = plan.get("phase_seconds") or campaign.get("phase_seconds") or {
        "baseline": 30.0, "normal": 10.0, "slot": 1.0, "recovery": 20.0,
    }
    expected_submissions = {
        "baseline": 0,
        "normal": int(campaign.get("normal_target_tx", 200)),
        "slot": len(schedule),
        "recovery": int(campaign.get("recovery_target_tx", 1)),
    }
    parsed_wrappers: dict[str, dict[str, Any]] = {}
    if not reasons:
        for position, wrapper in enumerate(episodes, start=1):
            role = wrapper["role"]
            episode = wrapper.get("episode") or {}
            validation = episode.get("validation") or {}
            if episode.get("role") != role:
                reasons.append(f"{role} wrapper and episode roles differ")
            if validation.get("role") not in (None, role):
                reasons.append(f"{role} wrapper and validation roles differ")
            if validation.get("status") != "validated":
                reasons.append(f"{role} validation is not validated")
            phases_list = episode.get("phases") or []
            if [phase.get("label") for phase in phases_list] != list(PHASE_ORDER):
                reasons.append(f"{role} phase order differs")
                continue
            phases = {phase["label"]: phase for phase in phases_list}
            previous_end = -1
            for label in PHASE_ORDER:
                phase = phases[label]
                try:
                    start_ns, end_ns = int(phase["start_ns"]), int(phase["end_ns"])
                    submitted, result = int(phase["submitted_frames"]), int(phase["result"])
                except (KeyError, TypeError, ValueError):
                    reasons.append(f"{role} {label} phase fields are incomplete")
                    continue
                if start_ns < previous_end or end_ns <= start_ns:
                    reasons.append(f"{role} {label} phase overlaps or is reversed")
                previous_end = end_ns
                expected_duration = float(phase_seconds[label])
                if abs((end_ns - start_ns) / 1e9 - expected_duration) > 0.1:
                    reasons.append(f"{role} {label} duration differs from campaign")
                if submitted != expected_submissions[label] or result != 1:
                    reasons.append(f"{role} {label} submission/result differs")
            target_counts = validation.get("target_tx_by_phase") or {}
            if any(int(target_counts.get(label, -1)) != expected for label, expected in expected_submissions.items()):
                reasons.append(f"{role} validation target_tx_by_phase differs")
            expected_schedule = [
                {"offset_ms": offset, "payload_hex": (payload if role == "mutation" else bytes.fromhex(str(plan["baseline_payload_hex"]))).hex().upper()}
                for offset, payload in schedule
            ]
            if validation.get("slot_schedule") is not None and validation.get("slot_schedule") != expected_schedule:
                reasons.append(f"{role} validation slot_schedule differs from plan")
            try:
                asc_name = locate_episode_asc(source, number, position, role)
            except ValueError as exc:
                reasons.append(str(exc))
                continue
            expected_hash = (episode.get("log_sha256") or {}).get("asc")
            if expected_hash and source.sha256(asc_name).lower() != str(expected_hash).lower():
                reasons.append(f"{role} ASC SHA-256 differs")
            parsed_wrappers[role] = {"episode": episode, "phases": phases, "asc_name": asc_name, "position": position}
    record = {
        "plan": plan, "complete": complete, "schedule": schedule,
        "legacy_schedule_inferred": legacy, "wrappers": parsed_wrappers,
    }
    return ("INVALID" if reasons else "VALID"), reasons, record


def verify_episode_capture(
    stats: EpisodeStats,
    plan: Mapping[str, Any],
    schedule: Sequence[ScheduleFrame],
    role: str,
    target_id: int,
    source_channel: int,
    campaign: Mapping[str, Any],
    timing_tolerance_ms: float,
) -> list[str]:
    reasons: list[str] = []
    expected_health = {(kind, channel) for kind in ("BusOff", "ErrorFrame") for channel in (1, 2, 3)}
    if not stats.header_valid or stats.asc_base not in ("dec", "hex"):
        reasons.append("ASC lacks supported base dec/hex absolute header")
    if stats.malformed_frames:
        reasons.append(f"ASC has {stats.malformed_frames} malformed frame-like lines")
    if stats.health_seen != expected_health:
        missing = sorted(expected_health - stats.health_seen)
        reasons.append(f"ASC lacks health counters: {missing}")
    if stats.health_nonzero:
        reasons.append(f"ASC has nonzero bus health: {stats.health_nonzero}")
    baseline = bytes.fromhex(str(plan["baseline_payload_hex"]))
    expected_by_phase: dict[str, list[ScheduleFrame]] = {
        "baseline": [],
        "normal": [(index * 50, baseline) for index in range(int(campaign.get("normal_target_tx", 200)))],
        "slot": [(offset, payload if role == "mutation" else baseline) for offset, payload in schedule],
        "recovery": [(0, baseline)] * int(campaign.get("recovery_target_tx", 1)),
    }
    for label, expected in expected_by_phase.items():
        observed = stats.target_tx.get(label, [])
        if len(observed) != len(expected):
            reasons.append(f"{role} {label} target Tx count={len(observed)}, expected={len(expected)}")
            continue
        observed_payloads = [payload for _, payload in observed]
        expected_payloads = [payload.hex().upper() for _, payload in expected]
        if observed_payloads != expected_payloads:
            reasons.append(f"{role} {label} target Tx payload/order differs")
        if label == "slot" and observed:
            observed_offsets = [(timestamp - observed[0][0]) * 1000.0 for timestamp, _ in observed]
            planned_offsets = [float(offset) for offset, _ in expected]
            if any(abs(actual - planned) > timing_tolerance_ms for actual, planned in zip(observed_offsets, planned_offsets)):
                reasons.append(f"{role} slot target Tx offsets drift from plan")
            start_s = float(stats.phases["slot"]["start_ns"]) / 1e9
            first_delay = (observed[0][0] - start_s) * 1000.0
            if not -1e-6 <= first_delay <= 75.0 + 1e-6:
                reasons.append(f"{role} slot first target Tx delay={first_delay:.3f}ms")
    outside = stats.target_tx.get("outside", [])
    if outside:
        reasons.append(f"{role} has {len(outside)} target Tx frames outside phases")
    return reasons


def is_isotp_transport(payloads: Mapping[str, int]) -> tuple[bool, int, bool]:
    if not payloads:
        return False, 0, False
    nibbles = [bytes.fromhex(payload)[0] >> 4 for payload in payloads if payload]
    if not nibbles:
        return False, 0, False
    sessions = sum(count for payload, count in payloads.items() if bytes.fromhex(payload)[0] >> 4 == 1)
    nibble_set = set(nibbles)
    continuation_only = nibble_set.issubset({2, 3}) and 2 in nibble_set
    transport = nibble_set.issubset({1, 2, 3}) and 2 in nibble_set
    return transport, sessions, continuation_only


def add_finding(
    findings: list[Finding], detector: str, severity: str, confidence: str,
    case_number: int, window: str, subject: str, channels: list[int],
    detail: str, evidence: dict[str, Any],
) -> None:
    findings.append(Finding(
        finding_id=f"F{len(findings) + 1:05d}", detector=detector,
        severity=severity, confidence=confidence, case_number=case_number,
        window=window, subject=subject, channels=channels, detail=detail,
        evidence=evidence,
    ))


def non_target_rx_total(stats: EpisodeStats, window: str, channel: int, target_id: int) -> int:
    return stats.rx_totals.get(window, {}).get(channel, 0) - sum(
        count
        for key, count in stats.rx_counts.get(window, {}).items()
        if key[0] == channel and key[1] == target_id
    )


def numeric_counter_median(counts: Mapping[Any, int]) -> float | None:
    values: list[float] = []
    for value, count in counts.items():
        if not isinstance(value, (int, float)):
            return None
        values.extend([float(value)] * int(count))
    return median(values)


def build_control_models(
    episodes: Mapping[int, dict[str, EpisodeStats]], target_id: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    noops = [pair["noop"] for pair in episodes.values()]
    count_model: DefaultDict[tuple[str, FrameKey], list[int]] = collections.defaultdict(list)
    delta_model: DefaultDict[tuple[str, FrameKey], list[int]] = collections.defaultdict(list)
    rate_model: DefaultDict[tuple[str, int], list[int]] = collections.defaultdict(list)
    rate_delta_model: DefaultDict[tuple[str, int], list[int]] = collections.defaultdict(list)
    presence: Counter[tuple[str, FrameKey]] = collections.Counter()
    signal_values: DefaultDict[SignalKey, set[Any]] = collections.defaultdict(set)
    signal_delta_model: DefaultDict[tuple[str, SignalKey], list[float]] = collections.defaultdict(list)
    for noop in noops:
        for window in WINDOWS:
            for key, counts in noop.signal_counts.get(window, {}).items():
                signal_values[key].update(counts)
    for window in WINDOWS:
        keys = set().union(*(set(noop.rx_counts.get(window, {})) for noop in noops)) if noops else set()
        for key in sorted(keys):
            values = [noop.rx_counts.get(window, {}).get(key, 0) for noop in noops]
            count_model[(window, key)] = values
            presence[(window, key)] = sum(value > 0 for value in values)
        channels = set().union(*(set(noop.rx_totals.get(window, {})) for noop in noops)) if noops else set()
        for channel in sorted(channels):
            # Compare downstream receive-frame rate only.  Leaving the target
            # stimulus in the control median would create a systematic 10/20
            # frame bias in the one-second slot window.
            rate_model[(window, channel)] = [
                non_target_rx_total(noop, window, channel, target_id)
                for noop in noops
            ]
    for window in COMPARE_WINDOWS:
        reference = "normal1" if window == "slot1" else "normal5"
        keys = set().union(*(set(noop.rx_counts.get(window, {})) | set(noop.rx_counts.get(reference, {})) for noop in noops)) if noops else set()
        for key in sorted(keys):
            delta_model[(window, key)] = [
                noop.rx_counts.get(window, {}).get(key, 0) - noop.rx_counts.get(reference, {}).get(key, 0)
                for noop in noops
            ]
        channels = set().union(*(
            set(noop.rx_totals.get(window, {})) | set(noop.rx_totals.get(reference, {}))
            for noop in noops
        )) if noops else set()
        for channel in sorted(channels):
            rate_delta_model[(window, channel)] = [
                non_target_rx_total(noop, window, channel, target_id)
                - non_target_rx_total(noop, reference, channel, target_id)
                for noop in noops
            ]
        signal_keys = set().union(*(
            set(noop.signal_counts.get(reference, {})) | set(noop.signal_counts.get(window, {}))
            for noop in noops
        )) if noops else set()
        for key in sorted(signal_keys):
            for noop in noops:
                before = numeric_counter_median(noop.signal_counts.get(reference, {}).get(key, {}))
                after = numeric_counter_median(noop.signal_counts.get(window, {}).get(key, {}))
                if before is not None and after is not None:
                    signal_delta_model[(window, key)].append(after - before)
    periodic: set[FrameKey] = set()
    periods: dict[FrameKey, list[float]] = {}
    timing_max: dict[tuple[str, FrameKey], list[float]] = {}
    normal_keys = set().union(*(set(noop.gaps_ms.get("normal5", {})) for noop in noops)) if noops else set()
    for key in sorted(normal_keys):
        per_episode = [noop.gaps_ms.get("normal5", {}).get(key, []) for noop in noops]
        if not per_episode or any(len(gaps) < args.minimum_intervals for gaps in per_episode):
            continue
        all_gaps = [gap for gaps in per_episode for gap in gaps if gap > 0]
        expected, p95 = median(all_gaps), percentile(all_gaps, 0.95)
        if expected and p95 and p95 <= expected * args.timing_warning_ratio:
            periodic.add(key)
            periods[key] = all_gaps
    for window in COMPARE_WINDOWS:
        for key in sorted(periodic):
            values = [noop.gaps_ms.get(window, {}).get(key, []) for noop in noops]
            if values and all(len(gaps) >= args.minimum_intervals for gaps in values):
                timing_max[(window, key)] = [max(gaps) for gaps in values]
    return {
        "noops": noops, "count": count_model, "delta": delta_model,
        "rate": rate_model, "rate_delta": rate_delta_model,
        "presence": presence, "periodic": periodic,
        "periods": periods, "timing_max": timing_max,
        "signal_values": signal_values, "signal_delta": signal_delta_model,
    }


def analyze_reactions(
    episodes: Mapping[int, dict[str, EpisodeStats]],
    plans: Mapping[int, Mapping[str, Any]],
    background_ids: set[FrameKey],
    target_id: int,
    decoder: SignalDecoder,
    args: argparse.Namespace,
) -> tuple[list[Finding], dict[str, int]]:
    findings: list[Finding] = []
    coverage = collections.Counter()
    models = build_control_models(episodes, target_id, args)
    noops: list[EpisodeStats] = models["noops"]
    for number, pair in sorted(episodes.items()):
        mutation, noop = pair["mutation"], pair["noop"]
        # Signal-level paired differential. Prestate mismatch excludes only that
        # signal, never the entire otherwise-valid case.
        for window in COMPARE_WINDOWS:
            reference = "normal1" if window == "slot1" else "normal5"
            signal_keys = (
                set(mutation.signal_counts.get(reference, {}))
                | set(mutation.signal_counts.get(window, {}))
                | set(noop.signal_counts.get(reference, {}))
                | set(noop.signal_counts.get(window, {}))
            )
            for key in sorted(signal_keys):
                if key[1] == target_id or is_protected_signal(key[2]):
                    continue
                profile = decoder.profile(key[1], key[2])
                bit_length = profile.get("bit_length")
                special_values = set(profile.get("special_values") or set())
                categorical = bit_length is not None and int(bit_length) <= args.categorical_max_bits
                mut_pre = mutation.signal_counts.get(reference, {}).get(key, collections.Counter())
                no_pre = noop.signal_counts.get(reference, {}).get(key, collections.Counter())
                mut_now = mutation.signal_counts.get(window, {}).get(key, collections.Counter())
                no_now = noop.signal_counts.get(window, {}).get(key, collections.Counter())
                if min(sum(mut_pre.values()), sum(no_pre.values())) < 3:
                    coverage["signals_insufficient_prestate"] += 1
                    continue
                if total_variation(mut_pre, no_pre) > args.prestate_tvd_max:
                    coverage["signals_skipped_prestate"] += 1
                    continue
                coverage["signals_eligible"] += 1
                known_values = (
                    set(models["signal_values"].get(key, set()))
                    | set(mut_pre)
                    | set(no_pre)
                    | set(no_now)
                    | set(mutation.signal_counts.get("baseline5", {}).get(key, {}))
                )
                # Exact-value novelty is meaningful for small discrete state
                # domains.  Signals with many control values are treated as
                # continuous/drifting and evaluated by distribution instead.
                exact_novelty_eligible = len(known_values) <= args.novel_max_control_values
                paired_current_tvd = total_variation(mut_now, no_now)
                new_values = (
                    set(mut_now) - known_values
                    if exact_novelty_eligible and paired_current_tvd >= args.signal_tvd_warning
                    else set()
                )
                novel_found = False
                for value in sorted(new_values, key=str):
                    special_value = value in special_values
                    if not categorical and not special_value:
                        continue
                    frames = mut_now[value]
                    max_run = mutation.signal_max_runs.get(window, {}).get(key, {}).get(value, 0)
                    critical = special_value or any(word in key[2].lower() for word in CRITICAL_WORDS)
                    if max_run < args.persistence_frames and not critical:
                        continue
                    persistent_recovery = window == "recovery_late5" and any(
                        mutation.signal_max_runs.get(earlier, {}).get(key, {}).get(value, 0) >= args.persistence_frames
                        for earlier in ("slot1", "recovery_early5")
                    )
                    # One mutation/noop pair is sufficient for prioritization,
                    # not causal confirmation.  STRONG is reserved for replay
                    # or independent ECU/HMI/DTC evidence outside this script.
                    severity = "CANDIDATE" if max_run >= args.persistence_frames else "WARNING"
                    add_finding(
                        findings, "signal_novel_value", severity, "medium", number,
                        window, signal_key_text(key), [key[0]],
                        f"mutation-only value {value}; frames={frames}, max_run={max_run}"
                        + ("; persisted into late recovery" if persistent_recovery else ""),
                        {
                            "can_id": key[1], "signal": key[2], "value": value,
                            "frames": frames, "max_run": max_run,
                            "paired_current_tvd": paired_current_tvd,
                            "persistent_recovery": persistent_recovery,
                            "categorical": categorical,
                            "dbc_special_value": special_value,
                            "replay_required": True,
                        },
                    )
                    novel_found = True
                if novel_found or min(sum(mut_now.values()), sum(no_now.values())) < 3:
                    continue
                if not categorical:
                    before = numeric_counter_median(mut_pre)
                    after = numeric_counter_median(mut_now)
                    paired_before = numeric_counter_median(no_pre)
                    paired_after = numeric_counter_median(no_now)
                    controls = models["signal_delta"].get((window, key), [])
                    if (
                        before is None or after is None
                        or paired_before is None or paired_after is None
                        or len(controls) < args.drift_min_control_episodes
                    ):
                        coverage["continuous_drift_insufficient_control"] += 1
                        continue
                    d_mut = after - before
                    d_noop = paired_after - paired_before
                    low, high = min(controls), max(controls)
                    control_abs_limit = max(abs(value) for value in controls)
                    magnitude_excess = abs(d_mut) - control_abs_limit
                    effect = d_mut - d_noop
                    # Generic continuous signals may drift in either direction
                    # (GPS, temperature, load).  Learn the normal magnitude;
                    # signal-specific monotonic rules belong in an explicit
                    # domain policy rather than being guessed from a name.
                    if magnitude_excess < args.drift_margin_raw or abs(effect) < args.drift_margin_raw:
                        continue
                    scale = profile.get("scale")
                    add_finding(
                        findings, "signal_numeric_drift", "WARNING", "low", number,
                        window, signal_key_text(key), [key[0]],
                        f"raw median delta={d_mut:+.3f}, noop delta envelope={low:+.3f}..{high:+.3f}, "
                        f"normal |delta| limit={control_abs_limit:.3f}, paired effect={effect:+.3f}",
                        {
                            "can_id": key[1], "signal": key[2],
                            "mutation_delta_raw": d_mut, "noop_delta_raw": d_noop,
                            "noop_delta_envelope_raw": [low, high],
                            "noop_abs_delta_limit_raw": control_abs_limit,
                            "magnitude_excess_raw": magnitude_excess,
                            "effect_raw": effect, "dbc_scale": scale,
                            "effect_physical": effect * scale if isinstance(scale, (int, float)) else None,
                            "replay_required": True,
                        },
                    )
                    continue
                mut_tvd = total_variation(mut_pre, mut_now)
                noop_tvd = total_variation(no_pre, no_now)
                effect = mut_tvd - noop_tvd
                pre_mode, pre_share = dominant(mut_pre)
                now_mode, now_share = dominant(mut_now)
                no_pre_mode, no_pre_share = dominant(no_pre)
                no_now_mode, no_now_share = dominant(no_now)
                mut_run_ratio = max(
                    mutation.signal_max_runs.get(window, {}).get(key, {}).values(), default=0
                ) / max(1, sum(mut_now.values()))
                pre_run_ratio = max(
                    mutation.signal_max_runs.get(reference, {}).get(key, {}).values(), default=0
                ) / max(1, sum(mut_pre.values()))
                distribution_changed = (
                    mut_tvd >= args.signal_tvd_warning
                    and effect >= args.signal_tvd_effect
                    and noop_tvd < args.signal_tvd_warning
                    and (
                        now_mode != pre_mode
                        or abs(now_share - pre_share) >= 0.5
                        or (mut_run_ratio >= 0.9 and pre_run_ratio < 0.8)
                    )
                )
                if distribution_changed:
                    severity = (
                        "CANDIDATE"
                        if mut_tvd >= args.signal_tvd_candidate and effect >= 0.5
                        else "WARNING"
                    )
                    add_finding(
                        findings, "signal_distribution", severity, "medium", number,
                        window, signal_key_text(key), [key[0]],
                        f"TVD={mut_tvd:.3f}, noop TVD={noop_tvd:.3f}, effect={effect:.3f}; mode {pre_mode}->{now_mode}",
                        {
                            "can_id": key[1], "signal": key[2], "mutation_tvd": mut_tvd,
                            "noop_tvd": noop_tvd, "effect": effect, "pre_mode": pre_mode,
                            "current_mode": now_mode, "pre_dominant_share": pre_share,
                            "current_dominant_share": now_share, "noop_pre_mode": no_pre_mode,
                            "noop_current_mode": no_now_mode, "noop_pre_share": no_pre_share,
                            "noop_current_share": no_now_share, "stuck_ratio": mut_run_ratio,
                        },
                    )
                def transition_rate(stats: EpisodeStats, label: str, signal: SignalKey, count: Mapping[Any, int]) -> float:
                    total = sum(count.values())
                    return stats.signal_transitions.get(label, {}).get(signal, 0) / max(1, total - 1)
                mut_transition_effect = (
                    transition_rate(mutation, window, key, mut_now)
                    - transition_rate(mutation, reference, key, mut_pre)
                ) - (
                    transition_rate(noop, window, key, no_now)
                    - transition_rate(noop, reference, key, no_pre)
                )
                if (
                    min(sum(mut_now.values()), sum(no_now.values())) >= args.transition_min_frames
                    and abs(mut_transition_effect) >= args.transition_effect_warning
                ):
                    severity = "CANDIDATE" if abs(mut_transition_effect) >= args.transition_effect_candidate else "WARNING"
                    add_finding(
                        findings, "signal_transition", severity, "medium", number,
                        window, signal_key_text(key), [key[0]],
                        f"paired transition-rate effect={mut_transition_effect:+.3f}",
                        {"can_id": key[1], "signal": key[2], "transition_effect": mut_transition_effect},
                    )

        for window in COMPARE_WINDOWS:
            reference = "normal1" if window == "slot1" else "normal5"
            keys = set(mutation.rx_counts.get(window, {})) | set(mutation.rx_counts.get(reference, {}))
            for key in sorted(keys):
                if key[1] == target_id:
                    continue
                current = mutation.rx_counts.get(window, {}).get(key, 0)
                paired = noop.rx_counts.get(window, {}).get(key, 0)
                pre = mutation.rx_counts.get(reference, {}).get(key, 0)
                paired_pre = noop.rx_counts.get(reference, {}).get(key, 0)
                model = models["count"].get((window, key), [])
                # A genuinely new frame key must be absent from the reference and
                # every full pre-slot/background capture, not just a 5s slice.
                if (
                    current >= args.new_id_min_frames
                    and key not in background_ids
                    and paired == 0
                    and models["presence"].get((window, key), 0) == 0
                ):
                    transport, sessions, continuation_only = is_isotp_transport(
                        mutation.payload_counts.get(window, {}).get(key, {})
                    )
                    severity = (
                        "WARNING"
                        if (transport and sessions <= 1) or current < args.new_id_candidate_frames
                        else "CANDIDATE"
                    )
                    add_finding(
                        findings, "new_transport_id" if transport else "new_id",
                        severity, "low" if transport else "medium", number, window,
                        frame_key_text(key), [key[0]],
                        f"new frame key appeared {current} times"
                        + (
                            " as an ISO-TP continuation/one-session observation"
                            if transport and sessions <= 1 else ""
                        ),
                        {
                            "can_id": key[1], "extended": key[2], "count": current,
                            "transport_like": transport, "session_starts": sessions,
                            "continuation_only": continuation_only,
                        },
                    )
                    continue
                if not model:
                    continue
                low, high = min(model), max(model)
                outside = distance_from_range(current, low, high)
                paired_outside = distance_from_range(paired, low, high)
                typical = median(model) or 0.0
                material = max(args.count_margin_frames, math.ceil(typical * args.count_change_ratio))
                if high - low > material or outside < args.count_margin_frames or paired_outside > 0:
                    continue
                d_mut, d_noop = current - pre, paired - paired_pre
                effect = d_mut - d_noop
                if abs(effect) < args.count_margin_frames:
                    continue
                delta_values = models["delta"].get((window, key), [0])
                delta_outside = distance_from_range(d_mut, min(delta_values), max(delta_values))
                severity = "CANDIDATE" if outside >= material and abs(effect) >= material and delta_outside >= material else "WARNING"
                add_finding(
                    findings, "message_count", severity, "medium", number, window,
                    frame_key_text(key), [key[0]],
                    f"count={current}, noop envelope={low}..{high}, paired={paired}; D_mut={d_mut}, D_noop={d_noop}, E={effect}",
                    {"can_id": key[1], "extended": key[2], "count": current, "noop_envelope": [low, high], "paired_noop": paired, "mutation_delta": d_mut, "noop_delta": d_noop, "effect": effect, "delta_envelope": [min(delta_values), max(delta_values)]},
                )

        for window in COMPARE_WINDOWS:
            for key, gaps in sorted(mutation.gaps_ms.get(window, {}).items()):
                if key[1] == target_id or key not in models["periodic"] or len(gaps) < args.minimum_intervals:
                    continue
                paired_gaps = noop.gaps_ms.get(window, {}).get(key, [])
                max_controls = models["timing_max"].get((window, key), [])
                expected = median(models["periods"].get(key, []))
                if not expected or not max_controls or len(paired_gaps) < args.minimum_intervals:
                    continue
                maximum, paired_max, control_max = max(gaps), max(paired_gaps), max(max_controls)
                tolerance = max(0.1, expected * 0.01)
                if maximum <= control_max + tolerance or paired_max > control_max + tolerance:
                    continue
                if maximum >= expected * args.loss_gap_ratio + tolerance:
                    severity = "CANDIDATE"
                elif maximum > expected * args.timing_warning_ratio + tolerance:
                    severity = "WARNING"
                else:
                    continue
                add_finding(
                    findings, "timing_gap", severity, "medium", number, window,
                    frame_key_text(key), [key[0]],
                    f"max gap={maximum:.3f}ms, T={expected:.3f}ms, noop max={control_max:.3f}ms",
                    {"can_id": key[1], "extended": key[2], "max_gap_ms": maximum, "period_ms": expected, "noop_max_ms": control_max, "paired_noop_max_ms": paired_max},
                )

        # This is Rx frame rate, not physical bus load (DLC and stuffing are not
        # estimated). The target stimulus is removed before comparison.
        for window in COMPARE_WINDOWS:
            reference = "normal1" if window == "slot1" else "normal5"
            for channel in sorted(set(mutation.rx_totals.get(window, {})) | set(noop.rx_totals.get(window, {}))):
                mutation_total = non_target_rx_total(mutation, window, channel, target_id)
                noop_total = non_target_rx_total(noop, window, channel, target_id)
                mutation_pre = non_target_rx_total(mutation, reference, channel, target_id)
                noop_pre = non_target_rx_total(noop, reference, channel, target_id)
                controls = models["rate"].get((window, channel), [])
                if not controls:
                    continue
                center = median(controls)
                if not center or center <= 0:
                    continue
                low, high = min(controls), max(controls)
                if low <= mutation_total <= high:
                    continue
                deviation = abs(mutation_total - center) / center
                paired_deviation = abs(noop_total - center) / center
                d_mut = mutation_total - mutation_pre
                d_noop = noop_total - noop_pre
                effect = d_mut - d_noop
                effect_ratio = abs(effect) / center
                delta_controls = models["rate_delta"].get((window, channel), [])
                delta_outside = (
                    distance_from_range(d_mut, min(delta_controls), max(delta_controls))
                    if delta_controls else 0
                )
                if (
                    deviation <= args.rate_warning_ratio
                    or paired_deviation > args.rate_warning_ratio
                    or effect_ratio <= args.rate_warning_ratio
                    or delta_outside <= 0
                ):
                    continue
                severity = (
                    "CANDIDATE"
                    if min(deviation, effect_ratio) >= args.rate_candidate_ratio
                    else "WARNING"
                )
                add_finding(
                    findings, "rx_frame_rate", severity, "low", number, window,
                    f"CAN{channel}", [channel],
                    f"non-target Rx frames={mutation_total}, noop envelope={low}..{high}, "
                    f"deviation={deviation:.2%}, paired effect={effect:+d} ({effect_ratio:.2%})",
                    {
                        "channel": channel, "frames": mutation_total,
                        "noop_envelope": [low, high], "noop_median": center,
                        "paired_noop": noop_total, "deviation": deviation,
                        "mutation_delta": d_mut, "noop_delta": d_noop,
                        "effect": effect, "effect_ratio": effect_ratio,
                        "delta_envelope": [min(delta_controls), max(delta_controls)]
                        if delta_controls else None,
                    },
                )
    return findings, dict(coverage)


def correlate_findings(findings: Sequence[Finding]) -> list[dict[str, Any]]:
    groups: DefaultDict[tuple[Any, ...], list[Finding]] = collections.defaultdict(list)
    for item in findings:
        can_id = item.evidence.get("can_id")
        signal = item.evidence.get("signal")
        extended = item.evidence.get("extended")
        value = item.evidence.get("value")
        subject_key = re.sub(r"^CAN\d+:", "", item.subject) if can_id is not None else item.subject
        key = (
            item.case_number, item.detector, item.window, can_id,
            extended, signal, value, subject_key,
        )
        groups[key].append(item)
    events = []
    for index, group in enumerate(groups.values(), start=1):
        channels = sorted({channel for item in group for channel in item.channels})
        severity = highest_severity(item.severity for item in group)
        event = {
            "event_id": f"E{index:04d}",
            "case_number": group[0].case_number,
            "detector": group[0].detector,
            "window": group[0].window,
            "severity": severity,
            "confidence": group[0].confidence,
            "subject": re.sub(r"^CAN\d+:", "", group[0].subject),
            "channels": channels,
            "member_findings": [item.finding_id for item in group],
            "detail": group[0].detail + (
                f"; cross-channel aggregate on CAN{channels} (timestamp correlation not asserted)"
                if len(channels) > 1 else ""
            ),
        }
        if group[0].evidence.get("can_id") is not None:
            event["can_id"] = f"0x{int(group[0].evidence['can_id']):X}"
        if group[0].evidence.get("extended") is not None:
            event["extended"] = bool(group[0].evidence["extended"])
        if group[0].evidence.get("signal") is not None:
            event["signal"] = group[0].evidence["signal"]
        events.append(event)
    return events


def load_campaign(source: CampaignSource) -> tuple[str, dict[str, Any]]:
    name = source.one(lambda item: item.endswith("/campaign.json") or item == "campaign.json", "campaign.json")
    return name, source.read_json(name)


def make_decoder(args: argparse.Namespace, campaign: Mapping[str, Any]) -> SignalDecoder:
    if args.dbc:
        return CantoolsSignalDecoder(args.dbc, str(campaign.get("dbc_sha256") or "") or None)
    maps = list(args.signal_map)
    if maps:
        return AuditSignalDecoder(maps)
    decoder = SignalDecoder()
    decoder.warnings.append("No DBC or audit signal map supplied; frame-level detectors only.")
    return decoder


def analyze_campaign(
    source: CampaignSource,
    campaign: dict[str, Any],
    decoder: SignalDecoder,
    args: argparse.Namespace,
) -> dict[str, Any]:
    global_reasons: list[str] = []
    if campaign.get("workflow") != "canoe_paired_capture":
        global_reasons.append("campaign workflow is not canoe_paired_capture")
    target_id = parse_int(str(campaign.get("frame_id", "0x366")))
    source_channel = int(campaign.get("channel", 2))
    if args.expect_target_id is not None and args.expect_target_id != target_id:
        raise ValueError(f"manifest target 0x{target_id:X} differs from --expect-target-id")
    if args.expect_source_channel is not None and args.expect_source_channel != source_channel:
        raise ValueError(f"manifest source CAN{source_channel} differs from --expect-source-channel")
    case_numbers = discover_case_numbers(source, args.case_start, args.case_end)
    if not case_numbers:
        raise ValueError("No case directories in the requested range")
    case_records: dict[int, dict[str, Any]] = {}
    episodes: dict[int, dict[str, EpisodeStats]] = {}
    plans: dict[int, Mapping[str, Any]] = {}
    legacy_cases: list[int] = []
    for number in case_numbers:
        print(f"  case {number:04d}: metadata and capture validation", flush=True)
        data_status, reasons, metadata = validate_case_metadata(source, campaign, number)
        record: dict[str, Any] = {
            "case_number": number,
            "case": f"case_{number:04d}",
            "method": campaign.get("method"),
            "data_status": data_status,
            "reaction_status": "NOT_ANALYZED",
            "reasons": reasons,
            "role_order": None,
            "slot_schedule": [],
            "slot_frame_count": 0,
            "legacy_schedule_inferred": False,
            "finding_count": 0,
            "event_count": 0,
        }
        if metadata and metadata.get("plan"):
            plan = metadata["plan"]
            record.update({
                "case": plan.get("case", record["case"]),
                "method": plan.get("method", campaign.get("method")),
                "role_order": plan.get("order"),
                "slot_schedule": [
                    {"offset_ms": offset, "payload_hex": payload.hex().upper()}
                    for offset, payload in metadata.get("schedule", [])
                ],
                "slot_frame_count": len(metadata.get("schedule", [])),
                "legacy_schedule_inferred": bool(metadata.get("legacy_schedule_inferred")),
            })
        case_records[number] = record
        if data_status != "VALID" or not metadata:
            continue
        plan = metadata["plan"]
        schedule = metadata["schedule"]
        parsed: dict[str, EpisodeStats] = {}
        parse_reasons: list[str] = []
        for role in ("mutation", "noop"):
            wrapper = metadata["wrappers"].get(role)
            if not wrapper:
                parse_reasons.append(f"missing parsed {role} wrapper")
                continue
            try:
                stats = parse_episode(
                    source, wrapper["asc_name"], number, role, wrapper["phases"],
                    decoder, target_id, source_channel,
                )
                parse_reasons.extend(verify_episode_capture(
                    stats, plan, schedule, role, target_id, source_channel,
                    campaign, args.schedule_tolerance_ms,
                ))
                parsed[role] = stats
            except Exception as exc:
                parse_reasons.append(f"{role} ASC parse failed: {exc}")
        if parse_reasons or set(parsed) != {"mutation", "noop"}:
            record["data_status"] = "INVALID"
            record["reasons"] = parse_reasons
            continue
        if metadata["legacy_schedule_inferred"]:
            legacy_cases.append(number)
        episodes[number] = parsed
        plans[number] = plan

    reference_names = [
        name for name in source.names
        if "/reference_baseline/" in f"/{name}" and "/raw/" in name and name.lower().endswith(".asc")
    ]
    reference_ids, reference_warnings = scan_background_ids(source, reference_names)
    background_ids = set(reference_ids)
    for pair in episodes.values():
        for stats in pair.values():
            background_ids.update(stats.pre_ids)
    findings, detector_coverage = analyze_reactions(
        episodes, plans, background_ids, target_id, decoder, args,
    )
    events = correlate_findings(findings)
    events_by_case: DefaultDict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    findings_by_case: DefaultDict[int, list[Finding]] = collections.defaultdict(list)
    for event in events:
        events_by_case[event["case_number"]].append(event)
    for finding in findings:
        findings_by_case[finding.case_number].append(finding)
    for number, record in case_records.items():
        if record["data_status"] != "VALID":
            continue
        related = findings_by_case.get(number, [])
        record["reaction_status"] = highest_severity(item.severity for item in related)
        record["finding_count"] = len(related)
        record["event_count"] = len(events_by_case.get(number, []))
    data_counts = collections.Counter(record["data_status"] for record in case_records.values())
    reaction_counts = collections.Counter(
        record["reaction_status"] for record in case_records.values()
        if record["data_status"] == "VALID"
    )
    overall_reaction = (
        highest_severity(
            record["reaction_status"] for record in case_records.values()
            if record["data_status"] == "VALID"
        )
        if episodes else "NOT_ANALYZED"
    )
    parsed_episodes = [stats for pair in episodes.values() for stats in pair.values()]
    total_rx = sum(stats.total_rx_frames for stats in parsed_episodes)
    mapped_rx = sum(stats.mapped_rx_frames for stats in parsed_episodes)
    warnings = list(decoder.warnings) + reference_warnings
    if legacy_cases:
        warnings.append(
            "Legacy schema-v1 cases inferred a static slot schedule from mutation_payload_hex and recorded slot count."
        )
    return {
        "schema_version": 2,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": str(source.source),
        "source_provenance": {
            "repository": REPOSITORY_URL,
            "contract_branch": REPOSITORY_BRANCH,
            "analyzer_checkout": repository_revision(),
            "capture_contract_files": list(CAPTURE_CONTRACT_FILES),
            "note": (
                "The checkout revision identifies the analyzer implementation; "
                "it is not the campaign producer revision unless recorded separately."
            ),
        },
        "campaign": {
            "schema_version": campaign.get("schema_version", 1),
            "workflow": campaign.get("workflow"),
            "analysis_status": campaign.get("analysis_status"),
            "method": campaign.get("method"),
            "target_id": f"0x{target_id:X}",
            "source_channel": source_channel,
            "phase_seconds": campaign.get("phase_seconds"),
            "catalog_sha256": campaign.get("catalog_sha256"),
            "dbc_sha256": campaign.get("dbc_sha256"),
            "physical_transmit_confirmed": False,
            "global_reasons": global_reasons,
        },
        "scope": {
            "discovered_cases": case_numbers,
            "valid_cases": sorted(episodes),
            "legacy_schedule_inferred_cases": legacy_cases,
            "reference_baseline_asc": reference_names,
            "background_frame_keys": len(background_ids),
        },
        "decoder": {
            "kind": decoder.kind,
            "sources": decoder.sources,
            "warnings": warnings,
        },
        "signal_decoding": {
            "enabled": decoder.kind != "none",
            "kind": decoder.kind,
            "sources": decoder.sources,
        },
        "coverage": {
            "parsed_episodes": len(parsed_episodes),
            "total_rx_frames": total_rx,
            "mapped_rx_frames": mapped_rx,
            "mapped_rx_ratio": mapped_rx / total_rx if total_rx else 0.0,
            "malformed_frame_lines": sum(stats.malformed_frames for stats in parsed_episodes),
            "legacy_plan_count": len(legacy_cases),
            **detector_coverage,
        },
        "summary": {
            "overall_reaction_status": overall_reaction,
            "data_status_counts": dict(data_counts),
            "reaction_status_counts": dict(reaction_counts),
            "finding_counts": dict(collections.Counter(item.severity for item in findings)),
            "correlated_event_count": len(events),
        },
        "cases": [case_records[number] for number in sorted(case_records)],
        "findings": [item.as_dict() for item in findings],
        "correlated_events": events,
        "policy": {
            "schedule_tolerance_ms": args.schedule_tolerance_ms,
            "persistence_frames": args.persistence_frames,
            "new_id_min_frames": args.new_id_min_frames,
            "new_id_candidate_frames": args.new_id_candidate_frames,
            "novel_max_control_values": args.novel_max_control_values,
            "categorical_max_bits": args.categorical_max_bits,
            "continuous_drift_min_control_episodes": args.drift_min_control_episodes,
            "continuous_drift_margin_raw": args.drift_margin_raw,
            "count_margin_frames": args.count_margin_frames,
            "count_change_ratio": args.count_change_ratio,
            "timing_warning_ratio": args.timing_warning_ratio,
            "loss_gap_ratio": args.loss_gap_ratio,
            "prestate_tvd_max": args.prestate_tvd_max,
            "signal_tvd_warning": args.signal_tvd_warning,
            "signal_tvd_candidate": args.signal_tvd_candidate,
            "transition_effect_warning": args.transition_effect_warning,
            "transition_effect_candidate": args.transition_effect_candidate,
            "transition_min_frames": args.transition_min_frames,
            "rx_frame_rate_warning_ratio": args.rate_warning_ratio,
            "rx_frame_rate_candidate_ratio": args.rate_candidate_ratio,
            "automatic_strong_enabled": False,
        },
        "limitations": [
            "CAPL output and CANoe Tx logging do not prove physical transmission or ECU acceptance.",
            "A single mutation/noop pair can prioritize replay but cannot establish causality.",
            "No DBC-derived downstream causal map exists; signal changes remain differential observations.",
            "ISO-TP detection is a PCI-byte heuristic and does not fully reassemble diagnostic sessions.",
            "rx_frame_rate is not physical bus load; DLC, arbitration and bit stuffing are not estimated.",
            "Temporal reactions are aggregated over the slot; this version does not attribute a reaction to one sequence element.",
            "Automatic findings are capped at CANDIDATE; STRONG requires replay or independent ECU/HMI/DTC evidence.",
        ],
    }


def markdown_report(result: Mapping[str, Any]) -> str:
    summary = result["summary"]
    campaign = result["campaign"]
    checkout = result["source_provenance"]["analyzer_checkout"]
    revision = checkout.get("commit_sha") or "unavailable"
    branch = checkout.get("branch") or result["source_provenance"]["contract_branch"]
    lines = [
        "# CANoe2 Paired Fuzzing Reaction Analysis", "",
        f"- Source: `{result['source']}`",
        f"- Analyzer checkout: `{branch}` commit `{revision}`",
        f"- Method: `{campaign.get('method')}`; target `{campaign['target_id']}` on CAN{campaign['source_channel']}",
        f"- Overall reaction: **{summary['overall_reaction_status']}**",
        f"- Data status: `{summary['data_status_counts']}`",
        f"- Reaction status: `{summary['reaction_status_counts']}`",
        "- Physical transmit confirmed: **false** (CANoe/CAPL contract limitation)", "",
        "## Case summary", "",
        "| Case | Method / name | Data | Reaction | Events | Legacy plan |",
        "|---:|---|---|---|---:|---|",
    ]
    for case in result["cases"]:
        name = f"{case.get('method') or '-'} / {case.get('case') or '-'}"
        lines.append(
            f"| {case['case_number']} | {name} | {case['data_status']} | "
            f"{case['reaction_status']} | {case['event_count']} | "
            f"{'yes' if case['legacy_schedule_inferred'] else 'no'} |"
        )
    lines.extend(["", "## Correlated events", ""])
    if not result["correlated_events"]:
        lines.append("No reaction events crossed the configured thresholds.")
    else:
        lines.extend([
            "| Severity | Case | Detector | Window | Subject | Channels | Detail |",
            "|---|---:|---|---|---|---|---|",
        ])
        for event in result["correlated_events"]:
            detail = event["detail"].replace("|", "\\|")
            lines.append(
                f"| {event['severity']} | {event['case_number']} | {event['detector']} | "
                f"{event['window']} | `{event['subject']}` | {event['channels']} | {detail} |"
            )
    invalid = [case for case in result["cases"] if case["data_status"] != "VALID"]
    lines.extend(["", "## Data exclusions", ""])
    if not invalid:
        lines.append("All selected cases passed the offline capture checks.")
    else:
        for case in invalid:
            lines.append(f"- case {case['case_number']}: **{case['data_status']}** — {'; '.join(case['reasons'])}")
    lines.extend([
        "", "## Decision logic", "",
        "1. Validate campaign/plan/complete integrity and the exact ordered CAN2 Tx schedule.",
        "2. Keep `data_status` separate from `reaction_status`; excluded or invalid captures are not reactions.",
        "3. Remove the target ID from downstream detectors and compare mutation change against paired noop change.",
        "4. Use full reference/baseline history for background IDs and phase-conditioned noop envelopes.",
        "5. Correlate matching channel observations into one event; require replay before causal confirmation.",
        "", "## Coverage and limitations", "",
        f"- Decoder: `{result['decoder']['kind']}` ({result['decoder']['sources']})",
        f"- Mapped Rx ratio: {result['coverage']['mapped_rx_ratio']:.2%}",
    ])
    lines.extend(f"- {warning}" for warning in result["decoder"]["warnings"])
    lines.extend(f"- {item}" for item in result["limitations"])
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and analyze a CANoe2 paired fuzzing campaign from a ZIP "
            "or extracted directory. plan.json is the stimulus authority."
        )
    )
    parser.add_argument("source", type=Path, help="campaign ZIP or extracted campaign directory")
    parser.add_argument("--output-dir", type=Path, default=Path("analysis_canoe2_campaign"))
    parser.add_argument("--case-start", type=int, help="first case number (default: auto-discover)")
    parser.add_argument("--case-end", type=int, help="last case number (default: auto-discover)")
    decoder = parser.add_mutually_exclusive_group()
    decoder.add_argument(
        "--dbc", type=Path,
        help="DBC for signal decoding; SHA-256 must match campaign.json when declared",
    )
    decoder.add_argument(
        "--signal-map", type=Path, action="append", default=[],
        help="audit JSON with a top-level fields map; repeatable when --dbc is omitted",
    )
    parser.add_argument(
        "--expect-target-id", type=parse_int,
        help="fail if campaign target differs (for example 0x366)",
    )
    parser.add_argument(
        "--expect-source-channel", type=int,
        help="fail if campaign source channel differs (for example 2)",
    )
    parser.add_argument(
        "--schedule-tolerance-ms", type=float, default=3.0,
        help="maximum per-frame slot offset error relative to the first Tx (default: 3.0)",
    )
    parser.add_argument("--persistence-frames", type=int, default=3)
    parser.add_argument("--new-id-min-frames", type=int, default=3)
    parser.add_argument("--new-id-candidate-frames", type=int, default=5)
    parser.add_argument(
        "--novel-max-control-values", type=int, default=8,
        help="disable exact-value novelty above this control cardinality",
    )
    parser.add_argument(
        "--categorical-max-bits", type=int, default=4,
        help="maximum DBC bit length for categorical distribution detectors",
    )
    parser.add_argument(
        "--drift-min-control-episodes", type=int, default=5,
        help="minimum noop episodes for a numeric phase-delta envelope",
    )
    parser.add_argument(
        "--drift-margin-raw", type=float, default=2.0,
        help="minimum raw-value distance beyond the noop delta envelope",
    )
    parser.add_argument("--count-margin-frames", type=int, default=2)
    parser.add_argument("--count-change-ratio", type=float, default=0.20)
    parser.add_argument("--minimum-intervals", type=int, default=3)
    parser.add_argument("--timing-warning-ratio", type=float, default=1.10)
    parser.add_argument("--loss-gap-ratio", type=float, default=1.80)
    parser.add_argument("--prestate-tvd-max", type=float, default=0.10)
    parser.add_argument("--signal-tvd-warning", type=float, default=0.30)
    parser.add_argument("--signal-tvd-candidate", type=float, default=0.70)
    parser.add_argument(
        "--signal-tvd-effect", type=float, default=0.20,
        help="minimum mutation-minus-noop TVD effect",
    )
    parser.add_argument("--transition-effect-warning", type=float, default=0.30)
    parser.add_argument("--transition-effect-candidate", type=float, default=0.70)
    parser.add_argument("--transition-min-frames", type=int, default=10)
    parser.add_argument(
        "--rate-warning-ratio", type=float, default=0.02,
        help="non-target receive-frame-rate warning ratio (default: 2%%)",
    )
    parser.add_argument(
        "--rate-candidate-ratio", type=float, default=0.05,
        help="non-target receive-frame-rate candidate ratio (default: 5%%)",
    )
    return parser


def validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.case_start is not None and args.case_start < 1:
        parser.error("--case-start must be at least 1")
    if args.case_end is not None and args.case_end < 1:
        parser.error("--case-end must be at least 1")
    if args.case_start is not None and args.case_end is not None and args.case_end < args.case_start:
        parser.error("--case-end must be greater than or equal to --case-start")
    positive_names = (
        "schedule_tolerance_ms", "persistence_frames", "new_id_min_frames",
        "new_id_candidate_frames", "novel_max_control_values",
        "categorical_max_bits", "drift_min_control_episodes", "drift_margin_raw",
        "count_margin_frames", "minimum_intervals", "transition_min_frames",
        "timing_warning_ratio", "loss_gap_ratio",
    )
    if any(getattr(args, name) <= 0 for name in positive_names):
        parser.error("timing, frame-count, and interval thresholds must be positive")
    unit_names = (
        "count_change_ratio", "prestate_tvd_max", "signal_tvd_warning",
        "signal_tvd_candidate", "signal_tvd_effect",
        "transition_effect_warning", "transition_effect_candidate",
        "rate_warning_ratio", "rate_candidate_ratio",
    )
    if any(not 0 <= getattr(args, name) <= 1 for name in unit_names):
        parser.error("ratio and TVD thresholds must be between 0 and 1")
    if args.signal_tvd_candidate < args.signal_tvd_warning:
        parser.error("--signal-tvd-candidate must be >= --signal-tvd-warning")
    if args.new_id_candidate_frames < args.new_id_min_frames:
        parser.error("--new-id-candidate-frames must be >= --new-id-min-frames")
    if args.transition_effect_candidate < args.transition_effect_warning:
        parser.error("--transition-effect-candidate must be >= --transition-effect-warning")
    if args.rate_candidate_ratio < args.rate_warning_ratio:
        parser.error("--rate-candidate-ratio must be >= --rate-warning-ratio")
    if args.loss_gap_ratio <= args.timing_warning_ratio:
        parser.error("--loss-gap-ratio must be greater than --timing-warning-ratio")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_arguments(parser, args)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    requested = (
        f"{args.case_start or 'first'}..{args.case_end or 'last'}"
        if args.case_start is not None or args.case_end is not None
        else "all discovered cases"
    )
    print(f"Analyzing {args.source} ({requested})", flush=True)
    try:
        with CampaignSource(args.source) as source:
            _, campaign = load_campaign(source)
            signal_decoder = make_decoder(args, campaign)
            result = analyze_campaign(source, campaign, signal_decoder, args)
    except (FileNotFoundError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))

    json_path = args.output_dir / "analysis.json"
    report_path = args.output_dir / "report.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(markdown_report(result), encoding="utf-8")
    print(f"JSON: {json_path.resolve()}")
    print(f"Markdown: {report_path.resolve()}")
    print(f"Data status: {result['summary']['data_status_counts']}")
    print(f"Reaction status: {result['summary']['reaction_status_counts']}")
    print(f"Overall reaction: {result['summary']['overall_reaction_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
