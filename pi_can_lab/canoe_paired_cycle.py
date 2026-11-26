#!/usr/bin/env python3
"""Capture paired CANoe mutation/no-op episodes with the stage-1 timing contract.

Each case has two continuous 61-second CANoe recordings. A short passive probe
before each recording verifies the live source payload before any transmission.
Only a completed, validated pair advances the campaign. Detailed anomaly and
causal analysis remains an offline step.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from itertools import zip_longest
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any

import can

from can_common import ConfigurationError
from canoe_baseline import select_baseline
from canoe_capture import attach_existing_canoe, capture_measurement
from canoe_catalog import (
    DEFAULT_CONFIG, build_slot_frames, load_settings, select_mutation_cases,
)
from canoe_paired_validation import (
    _check_asc_health, _frame_signature, _recorded_phase_windows, _tx_timing,
    validate_episode, validate_passive_state_match, validate_probe,
    validate_resume_probe,
)
from canoe_schedule import CANoeScheduleInterface
from paired_cycle import CYCLE_FAMILIES


BASELINE_SECONDS = 30.0
NORMAL_SECONDS = 10.0
SLOT_SECONDS = 1.0
RECOVERY_SECONDS = 20.0
PROBE_SECONDS = 6.0
INTERVAL_SECONDS = 0.05
NORMAL_FRAMES = 200
STORAGE_PER_PAIR_BYTES = 180 * 1024 * 1024
STORAGE_RESERVE_BYTES = 1024 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("-m", "--method", choices=CYCLE_FAMILIES, default="signal_single")
    parser.add_argument("--channel", type=int, choices=(2,), required=True)
    parser.add_argument("--canoe-config", type=Path, required=True)
    parser.add_argument("--reference-baseline-log", type=Path,
                        help="use an existing completed reference BLF/ASC pair")
    parser.add_argument("--capture-reference-baseline", action="store_true",
                        help="capture one passive 30-second reference pair in a new campaign, or reuse it")
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--case-start", type=int, default=1)
    parser.add_argument("-n", "--cases", type=int, default=1)
    parser.add_argument("--probe-only", action="store_true",
                        help="validate a fresh passive 6-second CANoe capture without sending")
    parser.add_argument("--exclude-interrupted-case", type=int,
                        help="mark the audited interrupted case excluded after a passive recovery check")
    parser.add_argument("--accept-operator-door-open", action="store_true",
                        help="record the operator-reported door opening and require two matching passive probes before excluding case 2")
    parser.add_argument("-x", "--execute", action="store_true")
    return parser


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def _write_or_match(path: Path, value: object) -> None:
    if path.is_file():
        if _read_json(path) != value:
            raise ConfigurationError(f"Frozen campaign plan differs: {path}")
    else:
        _write_json(path, value)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _paired_order(seed: int, case_number: int) -> tuple[str, str]:
    first_bit = hashlib.sha256(f"{seed}:1".encode()).digest()[0] & 1
    mutation_first = bool(first_bit ^ ((case_number - 1) & 1))
    return ("mutation", "noop") if mutation_first else ("noop", "mutation")


def _disk_preflight(path: Path, remaining_pairs: int, config: Path) -> None:
    """Reserve capacity on every volume used by CANoe logging or copied logs."""
    try:
        import pythoncom
    except ImportError as exc:
        raise ConfigurationError("Windows pywin32 is required to inspect CANoe logging") from exc
    pythoncom.CoInitialize()
    application = None
    try:
        application = attach_existing_canoe()
        if os.path.normcase(os.path.realpath(str(application.Configuration.FullName))) != os.path.normcase(os.path.realpath(str(config))):
            raise ConfigurationError("CANoe opened a different configuration during disk preflight")
        blocks = list(application.Configuration.OnlineSetup.LoggingCollection)
        if len(blocks) != 2:
            raise ConfigurationError("Expected one CANoe BLF and one ASC logging block")
        logging_dirs = []
        for block in blocks:
            source = Path(str(block.FullName))
            if not source.is_absolute():
                source = config.parent / source
            source = source.resolve()
            if not source.parent.is_dir():
                raise ConfigurationError(f"CANoe logging directory is missing: {source.parent}")
            logging_dirs.append(source.parent)
        if {Path(str(block.FullName)).suffix.lower() for block in blocks} != {".blf", ".asc"}:
            raise ConfigurationError("Expected CANoe BLF and ASC logging blocks")
    finally:
        application = None
        pythoncom.CoUninitialize()
    required = STORAGE_RESERVE_BYTES + STORAGE_PER_PAIR_BYTES * remaining_pairs
    volumes = {candidate.anchor.casefold(): candidate for candidate in (path, *logging_dirs)}
    for location in volumes.values():
        available = shutil.disk_usage(location).free
        if available < required:
            raise RuntimeError(
                f"Insufficient free space on {location.anchor} for "
                f"{remaining_pairs} paired cases: {available / 2**30:.2f} GiB "
                f"available, {required / 2**30:.2f} GiB required by the capture reserve"
            )


def _phase_frames(payload: bytes, count: int) -> list[tuple[float, bytes]]:
    return [(index * INTERVAL_SECONDS, payload) for index in range(count)]


def _record_phase(path: Path, phase: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(phase, ensure_ascii=False, allow_nan=False) + "\n")
        output.flush()


def _verify_completed_case(case_dir: Path, plan: dict[str, Any]) -> None:
    """Do not skip an exposure unless its saved capture evidence is intact."""
    completed = case_dir / "complete.json"
    if not completed.is_file():
        raise RuntimeError(f"Paired case {plan['case_number']} has partial evidence")
    record = _read_json(completed)
    if (record.get("status") != "captured" or record.get("plan") != plan
            or not isinstance(record.get("episodes"), list)
            or len(record["episodes"]) != 2):
        raise RuntimeError(f"Paired case {plan['case_number']} has conflicting completion evidence")
    for position, role in enumerate(plan["order"], start=1):
        item = record["episodes"][position - 1]
        if not isinstance(item, dict) or item.get("role") != role:
            raise RuntimeError(f"Paired case {plan['case_number']} episode order differs")
        episode_dir = case_dir / f"episode_{position}_{role}"
        for kind, folder in (("probe", "probe"), ("episode", "capture")):
            capture = item.get(kind)
            result_file = episode_dir / f"{kind}.json"
            if (not isinstance(capture, dict) or not result_file.is_file()
                    or _read_json(result_file) != capture
                    or (capture.get("validation") or {}).get("status") != "validated"):
                raise RuntimeError(
                    f"Paired case {plan['case_number']} {role}/{kind} has invalid evidence"
                )
            hashes = capture.get("log_sha256") or {}
            for extension in ("blf", "asc"):
                try:
                    log = Path(capture[extension]).resolve(strict=True)
                except (KeyError, OSError, TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"Paired case {plan['case_number']} {role}/{kind} log is missing"
                    ) from exc
                if (log.parent != (episode_dir / folder / "raw").resolve()
                        or log.suffix.lower() != f".{extension}"
                        or hashes.get(extension) != _hash_file(log)):
                    raise RuntimeError(
                        f"Paired case {plan['case_number']} {role}/{kind} log changed"
                    )
            if Path(capture["blf"]).stem != Path(capture["asc"]).stem:
                raise RuntimeError(f"Paired case {plan['case_number']} BLF/ASC do not match")
        phases_file = episode_dir / "phases.jsonl"
        if not phases_file.is_file() or [json.loads(line) for line in phases_file.read_text(
                encoding="utf-8").splitlines()] != item["episode"].get("phases"):
            raise RuntimeError(f"Paired case {plan['case_number']} phase evidence changed")


def _verified_capture_result(episode_dir: Path, kind: str) -> dict[str, Any]:
    """Verify one saved validated capture without altering its evidence."""
    result_path = episode_dir / f"{kind}.json"
    result = _read_json(result_path)
    if not isinstance(result, dict) or (result.get("validation") or {}).get("status") != "validated":
        raise RuntimeError(f"Invalid saved {kind} result: {result_path}")
    raw_dir = (episode_dir / ("probe" if kind == "probe" else "capture") / "raw").resolve()
    hashes = result.get("log_sha256") or {}
    for extension in ("blf", "asc"):
        log = Path(result[extension]).resolve(strict=True)
        if (log.parent != raw_dir or log.suffix.lower() != f".{extension}"
                or hashes.get(extension) != _hash_file(log)):
            raise RuntimeError(f"Saved {kind} log changed: {log}")
    if Path(result["blf"]).stem != Path(result["asc"]).stem:
        raise RuntimeError(f"Saved {kind} BLF/ASC names differ: {result_path}")
    if kind == "episode":
        phase_path = episode_dir / "phases.jsonl"
        phases = [json.loads(line) for line in phase_path.read_text(encoding="utf-8").splitlines()]
        if phases != result.get("phases"):
            raise RuntimeError(f"Saved episode phase evidence changed: {phase_path}")
    return result


def _audit_interrupted_case(case_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Identify this exact failed exposure before allowing a separate passive gate."""
    if plan.get("case_number") != 2 or plan.get("order") != ["noop", "mutation"]:
        raise RuntimeError("Only the observed case 2 noop/mutation interruption can be excluded")
    if (case_dir / "complete.json").exists():
        raise RuntimeError("A completed paired case cannot be excluded")
    plan_path = case_dir / "plan.json"
    error_path = case_dir / "error.json"
    if _read_json(plan_path) != plan:
        raise RuntimeError("Interrupted case plan differs from the frozen catalogue")
    error = _read_json(error_path)
    if (not isinstance(error, dict) or error.get("status") != "interrupted"
            or error.get("type") != "CANoeScheduleError"
            or error.get("message") != "FuzzHarness heartbeat stopped"
            or error.get("completed_episodes") != 1
            or error.get("automatic_retransmission") is not False):
        raise RuntimeError("Interrupted case error does not match the observed failure")
    noop_dir = case_dir / "episode_1_noop"
    mutation_dir = case_dir / "episode_2_mutation"
    noop_probe = _verified_capture_result(noop_dir, "probe")
    noop = _verified_capture_result(noop_dir, "episode")
    mutation_probe = _verified_capture_result(mutation_dir, "probe")
    if noop.get("role") != "noop" or mutation_probe.get("phase", {}).get("label") != "probe":
        raise RuntimeError("Interrupted case first episode or second probe differs")
    phase_path = mutation_dir / "phases.jsonl"
    phases = [json.loads(line) for line in phase_path.read_text(encoding="utf-8").splitlines()]
    expected = (("baseline", 30.0, 0), ("normal", 10.0, 200), ("slot", 1.0, 20))
    if len(phases) != 3:
        raise RuntimeError("Interrupted mutation must have exactly three completed phases")
    previous_end = -1
    for phase, (label, duration, submitted) in zip(phases, expected):
        start_ns, end_ns = phase.get("start_ns"), phase.get("end_ns")
        if (phase.get("label") != label or phase.get("result") != 1
                or phase.get("submitted_frames") != submitted
                or type(start_ns) is not int or type(end_ns) is not int
                or start_ns <= previous_end or end_ns <= start_ns
                or abs((end_ns - start_ns) / 1e9 - duration) > 0.1):
            raise RuntimeError(f"Interrupted mutation {label} phase evidence differs")
        previous_end = end_ns
    raw_dir = mutation_dir / "capture" / "raw"
    blfs = list(raw_dir.glob("*.blf"))
    ascs = list(raw_dir.glob("*.asc"))
    if len(blfs) != 1 or len(ascs) != 1 or blfs[0].stem != ascs[0].stem:
        raise RuntimeError("Interrupted mutation lacks one matching BLF/ASC pair")
    blf, asc = blfs[0].resolve(strict=True), ascs[0].resolve(strict=True)
    with asc.open("rb") as source:
        source.seek(max(0, asc.stat().st_size - 256))
        if not source.read().rstrip().endswith(b"End TriggerBlock"):
            raise RuntimeError("Interrupted mutation ASC is not finalized")
    _check_asc_health(asc)
    windows = _recorded_phase_windows(asc)
    if len(windows) != 4:
        raise RuntimeError("Interrupted mutation ASC lacks its recovery phase marker")
    for index, phase in enumerate(phases):
        if (abs(windows[index][0] - phase["start_ns"] / 1e9) > .001
                or abs(windows[index][1] - phase["end_ns"] / 1e9) > .001):
            raise RuntimeError("Interrupted mutation ASC/COM phase markers differ")
    if (windows[3][0] <= windows[2][1]
            or not 0 < windows[3][1] - windows[3][0] < RECOVERY_SECONDS):
        raise RuntimeError("Interrupted mutation recovery did not end early")
    asc_bytes = asc.read_bytes()
    if (re.search(rb"::FuzzSchedule::RequestSeq\s*=\s*4\b", asc_bytes) is None
            or re.search(rb"::FuzzSchedule::Result\s*=\s*-20\b", asc_bytes) is None
            or re.search(rb"::FuzzSchedule::DoneSeq\s*=\s*4\b", asc_bytes) is None):
        raise RuntimeError("Interrupted mutation recovery cancellation is absent")

    baseline_payload = bytes.fromhex(plan["baseline_payload_hex"])
    mutation_payload = bytes.fromhex(plan["mutation_payload_hex"])
    if mutation_payload == baseline_payload:
        raise RuntimeError("Interrupted case mutation payload is not distinct")
    target_times: dict[str, list[float]] = {label: [] for label in ("normal", "slot", "recovery")}
    target_counts = {label: 0 for label in target_times}
    with can.BLFReader(blf) as blf_reader, can.ASCReader(asc, relative_timestamp=True) as asc_reader:
        if blf_reader.file_size != blf.stat().st_size:
            raise RuntimeError("Interrupted mutation BLF size is invalid")
        origin = blf_reader.start_timestamp
        for index, (bm, am) in enumerate(zip_longest(blf_reader, asc_reader), start=1):
            if bm is None or am is None or _frame_signature(bm) != _frame_signature(am):
                raise RuntimeError(f"Interrupted mutation BLF/ASC frame {index} differs")
            seconds = bm.timestamp - origin
            if abs(seconds - am.timestamp) > .01 or bm.is_error_frame:
                raise RuntimeError(f"Interrupted mutation frame {index} timing/error differs")
            if bm.is_rx:
                continue
            if (bm.channel != 1 or bm.arbitration_id != 0x366
                    or bm.is_extended_id or bm.is_remote_frame or bm.is_fd
                    or bm.dlc != 8 or len(bm.data) != 8):
                raise RuntimeError("Interrupted mutation contains unexpected CANoe Tx")
            label = next((key for key, (start, end) in zip(
                ("baseline", "normal", "slot", "recovery"), windows
            ) if start <= seconds < end), None)
            if label not in target_counts:
                raise RuntimeError("Interrupted mutation target Tx lies outside expected phase")
            expected_payload = mutation_payload if label == "slot" else baseline_payload
            if bytes(bm.data) != expected_payload:
                raise RuntimeError(f"Interrupted mutation {label} Tx payload differs")
            target_counts[label] += 1
            target_times[label].append(seconds)
    if target_counts != {"normal": 200, "slot": 20, "recovery": 1}:
        raise RuntimeError(f"Interrupted mutation target Tx counts differ: {target_counts}")
    for label, index in (("normal", 1), ("slot", 2), ("recovery", 3)):
        _tx_timing(
            target_times[label],
            {"label": label, "start_s": windows[index][0], "end_s": windows[index][1]},
            periodic=label != "recovery",
        )
    return {
        "status": "audited_interruption",
        "case_number": 2,
        "plan_sha256": _hash_file(plan_path),
        "error_sha256": _hash_file(error_path),
        "first_noop_probe_sha256": _hash_file(noop_dir / "probe.json"),
        "first_noop_episode_sha256": _hash_file(noop_dir / "episode.json"),
        "second_mutation_probe_sha256": _hash_file(mutation_dir / "probe.json"),
        "second_mutation_phases_sha256": _hash_file(phase_path),
        "partial_blf": str(blf), "partial_blf_sha256": _hash_file(blf),
        "partial_asc": str(asc), "partial_asc_sha256": _hash_file(asc),
        "target_tx_by_phase": target_counts,
        "recovery_observed_seconds": round(windows[3][1] - windows[3][0], 6),
        "reference_normal_phase": phases[1],
        "first_noop_validation_status": noop["validation"]["status"],
        "first_noop_probe_status": noop_probe["validation"]["status"],
    }


def _verify_excluded_case(case_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    marker_path = case_dir / "excluded.json"
    marker = _read_json(marker_path)
    if (not isinstance(marker, dict) or marker.get("status") != "excluded"
            or marker.get("plan") != plan or marker.get("automatic_retransmission") is not False
            or (marker.get("resume_validation") or {}).get("status") != "validated"
            or marker.get("interruption_audit") != _audit_interrupted_case(case_dir, plan)):
        raise RuntimeError("Interrupted case exclusion marker or evidence changed")
    mode = marker.get("mode", "pre_exposure_match")
    if mode not in ("pre_exposure_match", "operator_reported_state_transition"):
        raise RuntimeError("Unknown interrupted case exclusion mode")
    probe_dir = Path(marker["recovery_probe_dir"]).resolve(strict=True)
    expected_parent = (
        case_dir / ("operator_state_checks" if mode == "operator_reported_state_transition"
                    else "exclusion_checks")
    ).resolve()
    if probe_dir.parent != expected_parent:
        raise RuntimeError("Exclusion probe is outside the interrupted case")
    probe = _verified_capture_result(probe_dir, "probe")
    if probe != marker.get("recovery_probe"):
        raise RuntimeError("Exclusion probe evidence changed")
    if mode == "operator_reported_state_transition":
        if (marker.get("operator_reported_door_open") is not True
                or marker.get("pre_exposure_state_match") is not False):
            raise RuntimeError("Operator-reported state transition record changed")
        reference_root = marker.get("new_state_reference_probe_root")
        if reference_root not in ("exclusion_checks", "operator_state_checks"):
            raise RuntimeError("Unknown operator state reference probe root")
        reference_dir = Path(marker["new_state_reference_probe_dir"]).resolve(strict=True)
        expected_reference_parent = case_dir.resolve() / reference_root
        if (reference_dir.parent != expected_reference_parent
                or reference_dir == probe_dir):
            raise RuntimeError("Operator state reference probe is outside the prior checks")
        reference = _verified_capture_result(reference_dir, "probe")
        if reference != marker.get("new_state_reference_probe"):
            raise RuntimeError("Operator state reference probe evidence changed")
    return marker


def _latest_prior_validated_probe(case_dir: Path, baseline_payload: bytes) -> tuple[Path, dict[str, Any]]:
    """Use the newest saved passive probe; failed raw-only attempts are ignored."""
    candidates: list[Path] = []
    for root_name in ("exclusion_checks", "operator_state_checks"):
        root = case_dir / root_name
        if not root.is_dir():
            continue
        if root.resolve() != case_dir.resolve() / root_name:
            raise RuntimeError("Prior probe root is outside the interrupted case")
        candidates.extend(
            path for path in root.iterdir()
            if path.is_dir() and (path / "probe.json").is_file()
        )
    candidates.sort(key=lambda path: path.name, reverse=True)
    if not candidates:
        raise RuntimeError("No prior validated passive probe is available")
    path = candidates[0].resolve(strict=True)
    if path.parent != case_dir.resolve() / candidates[0].parent.name:
        raise RuntimeError("Prior probe is outside the interrupted case")
    result = _verified_capture_result(path, "probe")
    expected_hex = baseline_payload.hex().upper()
    if (result.get("phase", {}).get("label") != "probe"
            or result.get("phase", {}).get("submitted_frames") != 0
            or (result.get("selection") or {}).get("payload_hex") != expected_hex
            or (result.get("validation", {}).get("baseline") or {}).get("payload_hex") != expected_hex):
        raise RuntimeError("Prior exclusion probe does not match the frozen baseline")
    return path, result


def _capture_probe(
    episode_dir: Path, config: Path, channel: int, frame_id: int,
    expected_payload: bytes,
) -> dict[str, Any]:
    phases: list[dict[str, Any]] = []

    def action(application: Any) -> None:
        with CANoeScheduleInterface(application, channel, config) as interface:
            phases.append(interface.run_phase("probe", PROBE_SECONDS, [], frame_id))

    blf, asc = capture_measurement(
        episode_dir / "probe" / "raw", 0, expected_config=config, action=action
    )
    selection = select_baseline(blf, channel, frame_id)
    if selection.payload != expected_payload:
        raise RuntimeError(
            f"Live source payload changed: {selection.payload_hex} "
            f"!= {expected_payload.hex().upper()}"
        )
    validation = validate_probe(
        blf, asc, channel=channel, frame_id=frame_id,
        expected_payload=expected_payload, phase=phases[0],
    )
    result = {
        "phase": phases[0],
        "selection": selection.to_record(),
        "validation": validation,
        "blf": str(blf), "asc": str(asc),
        "log_sha256": {"blf": _hash_file(blf), "asc": _hash_file(asc)},
    }
    _write_json(episode_dir / "probe.json", result)
    return result


def _capture_episode(
    episode_dir: Path, config: Path, channel: int, frame_id: int,
    baseline_payload: bytes, slot_frames: tuple[tuple[float, bytes], ...], role: str,
) -> dict[str, Any]:
    phases: list[dict[str, Any]] = []
    marker_path = episode_dir / "phases.jsonl"
    schedules = (
        ("baseline", BASELINE_SECONDS, []),
        ("normal", NORMAL_SECONDS, _phase_frames(baseline_payload, NORMAL_FRAMES)),
        ("slot", SLOT_SECONDS, slot_frames),
        ("recovery", RECOVERY_SECONDS, [(0.0, baseline_payload)]),
    )

    def action(application: Any) -> None:
        with CANoeScheduleInterface(application, channel, config) as interface:
            for label, seconds, frames in schedules:
                phase = interface.run_phase(label, seconds, frames, frame_id)
                phases.append(phase)
                _record_phase(marker_path, phase)
                print(f"[PHASE] {role}/{label}: {seconds:g}s submitted={len(frames)}", flush=True)

    blf, asc = capture_measurement(
        episode_dir / "capture" / "raw", 0, expected_config=config, action=action
    )
    validation = validate_episode(
        blf, asc, phases, channel=channel, frame_id=frame_id,
        baseline_payload=baseline_payload, slot_frames=slot_frames, role=role,
    )
    result = {
        "role": role,
        "phases": phases,
        "validation": validation,
        "blf": str(blf), "asc": str(asc),
        "log_sha256": {"blf": _hash_file(blf), "asc": _hash_file(asc)},
    }
    _write_json(episode_dir / "episode.json", result)
    return result


def _case_plan(case_number: int, case: Any, order: tuple[str, str],
               campaign: dict[str, Any], settings: Any) -> dict[str, Any]:
    slot_frames = build_slot_frames(case, settings)
    expected_episode_tx = NORMAL_FRAMES + len(slot_frames) + 1
    return {
        "schema_version": 2,
        "case_number": case_number,
        "method": settings.method,
        "case": case.parameters["case"],
        "mutation_payload_hex": case.mutated_payload.hex().upper(),
        "mutation_slot_frames": [
            {"offset_ms": round(offset * 1000), "payload_hex": payload.hex().upper()}
            for offset, payload in slot_frames
        ],
        "slot_frame_count": len(slot_frames),
        "baseline_payload_hex": campaign["baseline_payload_hex"],
        "order": list(order),
        "catalog_sha256": campaign["catalog_sha256"],
        "dbc_sha256": campaign["dbc_sha256"],
        "phase_seconds": campaign["phase_seconds"],
        "probe_seconds": PROBE_SECONDS,
        "expected_can2_target_tx_per_episode": expected_episode_tx,
        "expected_can2_target_tx_per_pair": 2 * expected_episode_tx,
    }


def _planned_slot_frames(
    plan: dict[str, Any], role: str, baseline_payload: bytes,
) -> tuple[tuple[float, bytes], ...]:
    if role not in ("mutation", "noop"):
        raise ValueError("paired episode role must be mutation or noop")
    return tuple(
        (
            int(frame["offset_ms"]) / 1000.0,
            bytes.fromhex(frame["payload_hex"]) if role == "mutation" else baseline_payload,
        )
        for frame in plan["mutation_slot_frames"]
    )


def _read_reference_baseline(reference: Path, channel: int) -> Any:
    reference = reference.resolve(strict=True)
    asc = reference.with_suffix(".asc")
    if reference.suffix.lower() != ".blf" or not asc.is_file():
        raise ConfigurationError("paired campaign needs a completed BLF/ASC baseline pair")
    with asc.open("rb") as source:
        source.seek(max(0, asc.stat().st_size - 256))
        if not source.read().rstrip().endswith(b"End TriggerBlock"):
            raise ConfigurationError("reference baseline ASC has no final End TriggerBlock")
    return select_baseline(reference, channel, 0x366)


def _capture_or_reuse_reference(
    campaign_dir: Path, config: Path, channel: int,
) -> tuple[Path, Any]:
    """Freeze one passive reference pair; never recapture an incomplete one."""
    campaign_dir.mkdir(parents=True, exist_ok=True)
    root = campaign_dir / "reference_baseline"
    marker_path = root / "reference.json"
    raw_dir = root / "raw"
    if root.exists():
        if not marker_path.is_file() or not raw_dir.is_dir():
            raise ConfigurationError(
                "Reference baseline capture is incomplete; keep its logs and use a new campaign"
            )
        marker = _read_json(marker_path)
        if not isinstance(marker, dict) or marker.get("schema_version") != 1 or marker.get("status") != "captured":
            raise ConfigurationError("Saved reference baseline marker is invalid")
        names = (marker.get("blf_name"), marker.get("asc_name"))
        if not all(isinstance(name, str) and Path(name).name == name for name in names):
            raise ConfigurationError("Saved reference baseline filenames are invalid")
        blf = raw_dir / names[0]
        asc = raw_dir / names[1]
        if (blf.suffix.lower(), asc.suffix.lower()) != (".blf", ".asc") or blf.stem != asc.stem:
            raise ConfigurationError("Saved reference baseline pair differs")
        if not blf.is_file() or not asc.is_file() or set(raw_dir.iterdir()) != {blf, asc}:
            raise ConfigurationError("Saved reference baseline logs are missing or changed")
        if (_hash_file(blf) != marker.get("blf_sha256")
                or _hash_file(asc) != marker.get("asc_sha256")):
            raise ConfigurationError("Saved reference baseline log changed")
        if (marker.get("configuration") != str(config)
                or marker.get("channel") != channel
                or marker.get("frame_id") != "0x366"
                or marker.get("duration_seconds") != BASELINE_SECONDS):
            raise ConfigurationError("Saved reference baseline configuration differs")
        baseline = _read_reference_baseline(blf, channel)
        if (baseline.payload_hex != marker.get("baseline_payload_hex")
                or baseline.matching_frames != marker.get("matching_rx_frames")):
            raise ConfigurationError("Saved reference baseline payload changed")
        print(f"[REFERENCE BASELINE] reused CAN{channel} 0x366 {baseline.payload_hex}; BLF={blf}")
        return blf, baseline

    if any(campaign_dir.iterdir()):
        raise ConfigurationError("Automatic reference baseline requires an empty new campaign")
    _disk_preflight(campaign_dir, 1, config)
    root.mkdir()
    blf, asc = capture_measurement(raw_dir, BASELINE_SECONDS, expected_config=config)
    if blf.with_suffix(".asc") != asc:
        raise ConfigurationError("Captured reference baseline BLF/ASC pair differs")
    baseline = _read_reference_baseline(blf, channel)
    _write_json(marker_path, {
        "schema_version": 1,
        "status": "captured",
        "configuration": str(config),
        "channel": channel,
        "frame_id": "0x366",
        "duration_seconds": BASELINE_SECONDS,
        "blf_name": blf.name,
        "asc_name": asc.name,
        "blf_sha256": _hash_file(blf),
        "asc_sha256": _hash_file(asc),
        "baseline_payload_hex": baseline.payload_hex,
        "matching_rx_frames": baseline.matching_frames,
    })
    print(f"[REFERENCE BASELINE] captured 30s CAN{channel} 0x366 {baseline.payload_hex} "
          f"from {baseline.matching_frames} Rx frames; BLF={blf}")
    return blf, baseline


def run(args: argparse.Namespace) -> Path:
    if args.case_start < 1 or not 1 <= args.cases <= 20:
        raise ConfigurationError("paired case start must be >= 1 and batch size must be 1..20")
    if sum((bool(args.probe_only), bool(args.execute),
            args.exclude_interrupted_case is not None)) > 1:
        raise ConfigurationError("Choose only one of --probe-only, --exclude-interrupted-case, and -x")
    if args.exclude_interrupted_case is not None and args.exclude_interrupted_case != 2:
        raise ConfigurationError("Only the observed interrupted case 2 can be excluded")
    if args.accept_operator_door_open and args.exclude_interrupted_case != 2:
        raise ConfigurationError("--accept-operator-door-open requires --exclude-interrupted-case 2")
    if bool(args.reference_baseline_log) == bool(args.capture_reference_baseline):
        raise ConfigurationError(
            "Choose exactly one of --reference-baseline-log or --capture-reference-baseline"
        )
    config = args.canoe_config.resolve(strict=True)
    settings = load_settings(args)
    if (
        settings.frame_id != 0x366
        or settings.source_bus != "b_can"
        or settings.mutation_duration_seconds != SLOT_SECONDS
        or settings.interval_ms != 50.0
        or not settings.restore_original
        or settings.restore_count != 1
    ):
        raise ConfigurationError("paired CANoe mode requires the 0x366/50ms/1s stage-1 contract")
    if args.exclude_interrupted_case is not None and settings.method != "signal_single":
        raise ConfigurationError("the archived case-2 exclusion applies only to signal_single")
    campaign_dir = args.campaign_dir.resolve()
    if campaign_dir.exists() and not campaign_dir.is_dir():
        raise ConfigurationError("campaign path is not a directory")
    if args.capture_reference_baseline:
        if args.case_start != 1 and not (campaign_dir / "reference_baseline").exists():
            raise ConfigurationError("new automatic baseline campaign must start at case 1")
        reference, baseline = _capture_or_reuse_reference(
            campaign_dir, config, args.channel
        )
    else:
        reference = args.reference_baseline_log.resolve(strict=True)
        baseline = _read_reference_baseline(reference, args.channel)
    settings = replace(settings, baseline_payload=baseline.payload)
    selection = select_mutation_cases(settings, first_case=args.case_start)
    campaign_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 2,
        "workflow": "canoe_paired_capture",
        "analysis_status": "pending_offline_analysis",
        "configuration": str(config),
        "channel": args.channel,
        "frame_id": "0x366",
        "method": settings.method,
        "reference_baseline_log": str(reference),
        "reference_baseline_sha256": _hash_file(reference),
        "baseline_payload_hex": baseline.payload_hex,
        "catalog_sha256": selection.catalog_sha256,
        "dbc_sha256": selection.dbc_sha256,
        "total_available_cases": selection.total_available_cases,
        "phase_seconds": {
            "baseline": BASELINE_SECONDS, "normal": NORMAL_SECONDS,
            "slot": SLOT_SECONDS, "recovery": RECOVERY_SECONDS,
        },
        "probe_seconds": PROBE_SECONDS,
        "normal_target_tx": NORMAL_FRAMES,
        "recovery_target_tx": 1,
        "slot_target_tx": "per_case_plan",
    }
    _write_or_match(campaign_dir / "campaign.json", manifest)
    preview = {
        "workflow": "canoe_paired_capture",
        "case_start": selection.case_start,
        "case_end": selection.case_end,
        "catalog_sha256": selection.catalog_sha256,
        "minimum_pair_seconds_excluding_probe_and_overhead": 122,
        "cases": [
            _case_plan(case.mutation_id, case,
                       _paired_order(settings.random_seed, case.mutation_id), manifest,
                       settings)
            for case in selection.cases
        ],
    }
    _write_or_match(
        campaign_dir / "previews" / f"cases_{selection.case_start:04d}_{selection.case_end:04d}.json",
        preview,
    )
    print(f"[PAIRED PLAN] cases {selection.case_start}-{selection.case_end} "
          f"of {selection.total_available_cases}; 122s minimum per case + 12s probes + overhead")
    if args.exclude_interrupted_case is not None:
        excluded_number = args.exclude_interrupted_case
        excluded_selection = select_mutation_cases(
            replace(settings, max_cases=1), first_case=excluded_number
        )
        if (excluded_selection.catalog_sha256 != selection.catalog_sha256
                or excluded_selection.dbc_sha256 != selection.dbc_sha256):
            raise ConfigurationError("Interrupted case catalogue differs from campaign")
        excluded_case = excluded_selection.cases[0]
        excluded_plan = _case_plan(
            excluded_number, excluded_case,
            _paired_order(settings.random_seed, excluded_number), manifest, settings,
        )
        _verify_completed_case(
            campaign_dir / "case_0001",
            _case_plan(
                1,
                select_mutation_cases(replace(settings, max_cases=1), first_case=1).cases[0],
                _paired_order(settings.random_seed, 1), manifest, settings,
            ),
        )
        case_dir = campaign_dir / f"case_{excluded_number:04d}"
        if (case_dir / "excluded.json").is_file():
            _verify_excluded_case(case_dir, excluded_plan)
            print(f"[EXCLUDED] case {excluded_number} already has a verified exclusion; no Tx")
            return campaign_dir
        audit = _audit_interrupted_case(case_dir, excluded_plan)
        prior_probe_dir = prior_probe = None
        if args.accept_operator_door_open:
            prior_probe_dir, prior_probe = _latest_prior_validated_probe(
                case_dir, baseline.payload
            )
        _disk_preflight(campaign_dir, 1, config)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        recovery_probe_dir = case_dir / (
            "operator_state_checks" if args.accept_operator_door_open else "exclusion_checks"
        ) / stamp
        probe = _capture_probe(
            recovery_probe_dir, config, args.channel, settings.frame_id, baseline.payload
        )
        if args.accept_operator_door_open:
            assert prior_probe_dir is not None and prior_probe is not None
            validation = validate_passive_state_match(
                Path(prior_probe["blf"]), Path(prior_probe["asc"]), prior_probe["phase"],
                Path(probe["blf"]), Path(probe["asc"]), probe["phase"],
                channel=args.channel, frame_id=settings.frame_id,
                baseline_payload=baseline.payload,
            )
        else:
            validation = validate_resume_probe(
                Path(audit["partial_blf"]), [audit["reference_normal_phase"]],
                Path(probe["blf"]), Path(probe["asc"]), probe["phase"],
                channel=args.channel, frame_id=settings.frame_id,
                baseline_payload=baseline.payload,
            )
        if validation.get("status") != "validated":
            raise RuntimeError("Passive recovery state comparison was not validated")
        marker = {
            "schema_version": 1,
            "status": "excluded",
            "case_number": excluded_number,
            "plan": excluded_plan,
            "reason": "interrupted after mutation slot; incomplete recovery capture",
            "mode": ("operator_reported_state_transition" if args.accept_operator_door_open
                     else "pre_exposure_match"),
            "interruption_audit": audit,
            "recovery_probe_dir": str(recovery_probe_dir.resolve()),
            "recovery_probe": probe,
            "resume_validation": validation,
            "automatic_retransmission": False,
            "recorded_at_utc": stamp,
        }
        if args.accept_operator_door_open:
            marker.update({
                "operator_reported_door_open": True,
                "operator_report": "A vehicle door was opened manually; exact timing unknown",
                "pre_exposure_state_match": False,
                "new_state_reference_probe_root": prior_probe_dir.parent.name,
                "new_state_reference_probe_dir": str(prior_probe_dir.resolve()),
                "new_state_reference_probe": prior_probe,
            })
        _write_json(case_dir / "excluded.json", marker)
        print(f"[EXCLUDED] case {excluded_number} remains incomplete; "
              + ("operator-reported new state matched two passive probes; no Tx"
                 if args.accept_operator_door_open else "passive state validated; no Tx"))
        return campaign_dir
    if args.probe_only:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        probe_dir = campaign_dir / "passive_probes" / stamp
        _disk_preflight(campaign_dir, 1, config)
        probe = _capture_probe(
            probe_dir, config, args.channel, settings.frame_id, baseline.payload
        )
        print(f"[PROBE VALIDATED] CAN1/2/3 Rx; source payload {baseline.payload_hex}; "
              f"BLF={probe['blf']}")
        return campaign_dir
    if not args.execute:
        print("[PREVIEW] Add -x only after reviewing the frozen pair plans.")
        return campaign_dir

    all_selected = select_mutation_cases(
        replace(settings, max_cases=selection.case_end), first_case=1
    )
    if (all_selected.catalog_sha256 != selection.catalog_sha256
            or all_selected.dbc_sha256 != selection.dbc_sha256):
        raise ConfigurationError("Full paired catalogue changed while preparing the batch")
    expected_plans = {
        case.mutation_id: _case_plan(
            case.mutation_id, case,
            _paired_order(settings.random_seed, case.mutation_id), manifest, settings,
        ) for case in all_selected.cases
    }
    for case_number in range(1, selection.case_start):
        case_dir = campaign_dir / f"case_{case_number:04d}"
        if (case_dir / "excluded.json").is_file():
            _verify_excluded_case(case_dir, expected_plans[case_number])
        else:
            _verify_completed_case(case_dir, expected_plans[case_number])
    for case in selection.cases:
        case_number = case.mutation_id
        case_dir = campaign_dir / f"case_{case_number:04d}"
        if not case_dir.exists():
            continue
        if (case_dir / "excluded.json").is_file():
            _verify_excluded_case(case_dir, expected_plans[case_number])
        else:
            _verify_completed_case(case_dir, expected_plans[case_number])
    pending = [case for case in selection.cases
               if not (campaign_dir / f"case_{case.mutation_id:04d}").exists()]
    if pending:
        _disk_preflight(campaign_dir, len(pending), config)
    for case in selection.cases:
        case_number = case.mutation_id
        case_dir = campaign_dir / f"case_{case_number:04d}"
        plan = expected_plans[case_number]
        if case_dir.exists():
            if (case_dir / "excluded.json").is_file():
                _verify_excluded_case(case_dir, plan)
                print(f"[EXCLUDED] paired case {case_number} is incomplete; no Tx")
            else:
                _verify_completed_case(case_dir, plan)
                print(f"[SKIP] paired case {case_number} already complete")
            continue
        _disk_preflight(campaign_dir, 1, config)
        case_dir.mkdir()
        _write_json(case_dir / "plan.json", plan)
        results: list[dict[str, Any]] = []
        try:
            for position, role in enumerate(plan["order"], start=1):
                episode_dir = case_dir / f"episode_{position}_{role}"
                episode_dir.mkdir()
                print(f"[PAIR {case_number}/{selection.total_available_cases}] "
                      f"{position}/2 {role}: passive probe, then 30/10/1/20s capture", flush=True)
                probe = _capture_probe(
                    episode_dir, config, args.channel, settings.frame_id,
                    baseline.payload,
                )
                slot_frames = _planned_slot_frames(plan, role, baseline.payload)
                episode = _capture_episode(
                    episode_dir, config, args.channel, settings.frame_id,
                    baseline.payload, slot_frames, role,
                )
                results.append({"role": role, "probe": probe, "episode": episode})
            _write_json(case_dir / "complete.json", {
                "status": "captured",
                "analysis_status": "pending_offline_analysis",
                "plan": plan,
                "episodes": results,
            })
            print(f"[PAIR DONE] {case_number}: mutation and no-op captured; "
                  f"CAN2 target Tx {plan['expected_can2_target_tx_per_pair']} expected",
                  flush=True)
        except BaseException as exc:
            _write_json(case_dir / "error.json", {
                "status": "interrupted",
                "type": type(exc).__name__,
                "message": str(exc),
                "completed_episodes": len(results),
                "partial_logs_may_exist": True,
                "automatic_retransmission": False,
            })
            raise
    excluded = [case.mutation_id for case in selection.cases
                if (campaign_dir / f"case_{case.mutation_id:04d}" / "excluded.json").is_file()]
    print(f"[BATCH DONE] cases {selection.case_start}-{selection.case_end}: "
          f"{len(selection.cases) - len(excluded)} paired captures, "
          f"{len(excluded)} excluded; detailed offline analysis pending")
    return campaign_dir


def main() -> int:
    args = build_parser().parse_args()
    try:
        run(args)
    except (ConfigurationError, OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(f"[ERROR] {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
