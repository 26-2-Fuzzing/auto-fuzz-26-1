"""Offline evidence checks for one CANoe paired episode and its pre-probe."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Sequence

import can

from canoe_paired_validation import (
    EpisodeValidationError,
    _phase_for_time,
    validate_episode,
    validate_probe,
    validate_passive_state_match,
    validate_resume_probe,
)


ORIGINAL = bytes.fromhex("00000000200000F0")
MUTATED = bytes.fromhex("00000000240000F0")
MUTATED_B = bytes.fromhex("00000000280000F0")
MUTATED_C = bytes.fromhex("000000002C0000F0")
EPOCH = 1_700_000_000.0
STATIC_MUTATION_FRAMES = [(index * .05, MUTATED) for index in range(20)]
STATIC_NOOP_FRAMES = [(index * .05, ORIGINAL) for index in range(20)]
TEMPORAL_100MS_FRAMES = [
    (index * .1, (MUTATED, MUTATED_B, MUTATED_C)[index % 3])
    for index in range(10)
]


def message(seconds: float, channel: int, frame_id: int, data: bytes, *, rx=True) -> can.Message:
    return can.Message(
        timestamp=EPOCH + seconds,
        channel=channel,
        arbitration_id=frame_id,
        data=data,
        is_rx=rx,
        is_extended_id=False,
    )


def phase_records(slot_count: int = 20) -> list[dict]:
    boundaries = (
        ("baseline", .1, 30.1, 0),
        ("normal", 30.2, 40.2, 200),
        ("slot", 40.3, 41.3, slot_count),
        ("recovery", 41.4, 61.4, 1),
    )
    return [
        {
            "label": name,
            "start_ns": round(start * 1e9),
            "end_ns": round(end * 1e9),
            "submitted_frames": count,
            "result": 1,
        }
        for name, start, end, count in boundaries
    ]


def probe_phase() -> dict:
    return {
        "label": "probe", "start_ns": 100_000_000,
        "end_ns": 6_100_000_000, "submitted_frames": 0, "result": 1,
    }


def probe_messages() -> list[can.Message]:
    frames = [
        message(0, 0, 0x100, b"\0" * 8),
        message(.5, 0, 0x100, b"\0" * 8),
        message(.5, 2, 0x200, b"\0" * 8),
        message(1, 1, 0x366, ORIGINAL),
        message(2, 1, 0x366, ORIGINAL),
        message(3, 1, 0x366, ORIGINAL),
        message(4, 1, 0x366, ORIGINAL),
        message(5, 1, 0x366, ORIGINAL),
        message(6, 1, 0x366, ORIGINAL),
    ]
    for index in range(5):
        seconds = 1.2 + index * .9
        frames.extend((
            message(seconds, 0, 0x583, b"\0" * 8),
            message(seconds, 1, 0x3D6, b"\0" * 8),
            message(seconds, 2, 0x3CE, b"\0" * 8),
        ))
    return sorted(frames, key=lambda frame: frame.timestamp)


def episode_messages(
    slot_payload: bytes, *, slot_frames: Sequence[tuple[float, bytes]] | None = None,
) -> list[can.Message]:
    frames = [
        message(0, 0, 0x100, b"\0" * 8),
        message(.5, 2, 0x200, b"\0" * 8),
        message(1, 1, 0x366, ORIGINAL),
        message(2, 1, 0x366, ORIGINAL),
        message(3, 1, 0x366, ORIGINAL),
        message(60, 1, 0x366, ORIGINAL),
    ]
    for late_start in (25.1, 35.2, 56.4):
        frames += [
            message(late_start + offset, 1, 0x366, ORIGINAL)
            for offset in (.5, 1.5, 2.5)
        ]
        for index in range(5):
            offset = .2 + index * .9
            frames.extend((
                message(late_start + offset, 0, 0x583, b"\0" * 8),
                message(late_start + offset, 1, 0x3D6, b"\0" * 8),
                message(late_start + offset, 2, 0x3CE, b"\0" * 8),
            ))
    frames += [message(30.201 + i * .05, 1, 0x366, ORIGINAL, rx=False) for i in range(200)]
    if slot_frames is None:
        slot_frames = [(index * .05, slot_payload) for index in range(20)]
    frames += [
        message(40.301 + offset, 1, 0x366, payload, rx=False)
        for offset, payload in slot_frames
    ]
    frames.append(message(41.401, 1, 0x366, ORIGINAL, rx=False))
    return sorted(frames, key=lambda frame: frame.timestamp)


class EpisodeValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_pair(self, frames, *, asc_frames=None, markers=True, health=True,
                   marker_phases=None, stem="capture"):
        blf = self.root / f"{stem}.blf"
        asc = self.root / f"{stem}.asc"
        with can.BLFWriter(blf) as writer:
            for frame in frames:
                writer.on_message_received(frame)
        with can.ASCWriter(asc) as writer:
            for frame in asc_frames if asc_frames is not None else frames:
                writer.on_message_received(frame)
        content = asc.read_bytes().replace(b"Begin Triggerblock", b"Begin TriggerBlock")
        extra = []
        if health:
            for name in ("BusOff", "ErrorFrame"):
                for channel in (1, 2, 3):
                    extra.append(f" 0.000000    SV: 6 0 1 ::FuzzIO::{name}{channel} = 0\n".encode())
        if markers:
            for phase in marker_phases if marker_phases is not None else phase_records():
                for key, seconds in (("StartNs", phase["start_ns"]), ("EndNs", phase["end_ns"])):
                    extra.append(
                        f" {seconds/1e9:.6f}    SV: 6 0 1 ::FuzzSchedule::{key} = L{seconds}\n".encode()
                    )
                    if key == "StartNs":
                        # CAPL clears EndNs at each request, after its new
                        # positive StartNs may already be logged.
                        extra.append(
                            f" {seconds/1e9:.6f}    SV: 6 0 1 ::FuzzSchedule::EndNs = L0\n".encode()
                        )
        content = content.replace(b"End TriggerBlock", b"".join(extra) + b"End TriggerBlock")
        asc.write_bytes(content)
        return blf, asc

    def validate_mutation(self, frames):
        blf, asc = self.write_pair(sorted(frames, key=lambda frame: frame.timestamp))
        return validate_episode(
            blf, asc, phase_records(), channel=2, frame_id=0x366,
            baseline_payload=ORIGINAL, slot_frames=STATIC_MUTATION_FRAMES, role="mutation",
        )

    def validate_resume(self, reference_frames=None, probe_frames=None):
        reference_frames = (episode_messages(MUTATED) if reference_frames is None
                            else reference_frames)
        probe_frames = probe_messages() if probe_frames is None else probe_frames
        reference_blf, _ = self.write_pair(
            sorted(reference_frames, key=lambda frame: frame.timestamp), stem="reference",
        )
        probe_blf, probe_asc = self.write_pair(
            sorted(probe_frames, key=lambda frame: frame.timestamp),
            marker_phases=[probe_phase()], stem="probe",
        )
        return validate_resume_probe(
            reference_blf, phase_records()[:2], probe_blf, probe_asc,
            probe_phase(), channel=2, frame_id=0x366,
            baseline_payload=ORIGINAL,
        )

    def validate_two_passive(self, reference_frames=None, candidate_frames=None):
        reference_frames = probe_messages() if reference_frames is None else reference_frames
        candidate_frames = probe_messages() if candidate_frames is None else candidate_frames
        reference_blf, reference_asc = self.write_pair(
            sorted(reference_frames, key=lambda frame: frame.timestamp),
            marker_phases=[probe_phase()], stem="post_change_reference",
        )
        candidate_blf, candidate_asc = self.write_pair(
            sorted(candidate_frames, key=lambda frame: frame.timestamp),
            marker_phases=[probe_phase()], stem="post_change_candidate",
        )
        return validate_passive_state_match(
            reference_blf, reference_asc, probe_phase(), candidate_blf,
            candidate_asc, probe_phase(), channel=2, frame_id=0x366,
            baseline_payload=ORIGINAL,
        )

    def test_complete_mutation_episode_has_four_verified_windows(self):
        blf, asc = self.write_pair(episode_messages(MUTATED))
        result = validate_episode(
            blf, asc, phase_records(), channel=2, frame_id=0x366,
            baseline_payload=ORIGINAL, slot_frames=STATIC_MUTATION_FRAMES, role="mutation",
        )
        self.assertEqual(result["target_tx_by_phase"], {
            "baseline": 0, "normal": 200, "slot": 20, "recovery": 1,
        })
        self.assertGreaterEqual(result["baseline_target_rx"]["matching_original"], 6)
        self.assertTrue(all(result["rx_counts"].values()))
        self.assertEqual(result["source_late_target_rx"], {
            "baseline": 3, "normal": 3, "recovery": 4,
        })
        self.assertFalse(result["physical_transmit_confirmed"])

    def test_complete_noop_episode_uses_original_in_slot(self):
        blf, asc = self.write_pair(episode_messages(ORIGINAL))
        result = validate_episode(
            blf, asc, phase_records(), channel=2, frame_id=0x366,
            baseline_payload=ORIGINAL, slot_frames=STATIC_NOOP_FRAMES, role="noop",
        )
        self.assertEqual(result["target_tx_by_phase"]["slot"], 20)

    def test_temporal_100ms_slot_validates_exact_sequence_and_noop_control(self):
        phases = phase_records(10)
        blf, asc = self.write_pair(
            episode_messages(MUTATED, slot_frames=TEMPORAL_100MS_FRAMES),
            marker_phases=phases,
        )
        result = validate_episode(
            blf, asc, phases, channel=2, frame_id=0x366,
            baseline_payload=ORIGINAL, slot_frames=TEMPORAL_100MS_FRAMES,
            role="mutation",
        )
        self.assertEqual(result["target_tx_by_phase"]["slot"], 10)
        self.assertEqual(result["target_tx_timing_ms"]["slot"]["min_gap_ms"], 100.0)
        self.assertEqual(result["slot_schedule"][1], {
            "offset_ms": 100, "payload_hex": MUTATED_B.hex().upper(),
        })

        control = [(offset, ORIGINAL) for offset, _ in TEMPORAL_100MS_FRAMES]
        blf, asc = self.write_pair(
            episode_messages(ORIGINAL, slot_frames=control), marker_phases=phases,
            stem="noop",
        )
        noop = validate_episode(
            blf, asc, phases, channel=2, frame_id=0x366,
            baseline_payload=ORIGINAL, slot_frames=control, role="noop",
        )
        self.assertEqual(noop["target_tx_by_phase"]["slot"], 10)

    def test_temporal_slot_rejects_wrong_payload_order(self):
        phases = phase_records(10)
        frames = episode_messages(MUTATED, slot_frames=TEMPORAL_100MS_FRAMES)
        slot_tx = [frame for frame in frames if not frame.is_rx
                   and 40.3 <= frame.timestamp - EPOCH < 41.3]
        slot_tx[1].data = MUTATED_C
        blf, asc = self.write_pair(frames, marker_phases=phases)
        with self.assertRaisesRegex(EpisodeValidationError, "slot at index 1"):
            validate_episode(
                blf, asc, phases, channel=2, frame_id=0x366,
                baseline_payload=ORIGINAL, slot_frames=TEMPORAL_100MS_FRAMES,
                role="mutation",
            )

    def test_temporal_slot_rejects_wrong_frequency(self):
        phases = phase_records(10)
        frames = episode_messages(MUTATED, slot_frames=TEMPORAL_100MS_FRAMES)
        slot_tx = [frame for frame in frames if not frame.is_rx
                   and 40.3 <= frame.timestamp - EPOCH < 41.3]
        slot_tx[5].timestamp += .04
        blf, asc = self.write_pair(sorted(frames, key=lambda frame: frame.timestamp),
                                   marker_phases=phases)
        with self.assertRaisesRegex(EpisodeValidationError, "non-100ms interval"):
            validate_episode(
                blf, asc, phases, channel=2, frame_id=0x366,
                baseline_payload=ORIGINAL, slot_frames=TEMPORAL_100MS_FRAMES,
                role="mutation",
            )

    def test_incomplete_slot_is_rejected_even_when_capl_reported_twenty(self):
        frames = episode_messages(MUTATED)
        for index, frame in enumerate(frames):
            if not frame.is_rx and frame.data == MUTATED:
                del frames[index]
                break
        blf, asc = self.write_pair(frames)
        with self.assertRaisesRegex(EpisodeValidationError, "slot has 19"):
            validate_episode(
                blf, asc, phase_records(), channel=2, frame_id=0x366,
                baseline_payload=ORIGINAL, slot_frames=STATIC_MUTATION_FRAMES, role="mutation",
            )

    def test_baseline_drift_and_missing_health_are_rejected(self):
        frames = episode_messages(MUTATED)
        for frame in frames:
            if (frame.is_rx and frame.arbitration_id == 0x366
                    and .1 <= frame.timestamp-EPOCH < 30.1):
                frame.data = MUTATED
        blf, asc = self.write_pair(frames)
        with self.assertRaisesRegex(EpisodeValidationError, "strict majority"):
            validate_episode(
                blf, asc, phase_records(), channel=2, frame_id=0x366,
                baseline_payload=ORIGINAL, slot_frames=STATIC_MUTATION_FRAMES, role="mutation",
            )
        blf, asc = self.write_pair(episode_messages(MUTATED), health=False)
        with self.assertRaisesRegex(EpisodeValidationError, "lacks complete"):
            validate_episode(
                blf, asc, phase_records(), channel=2, frame_id=0x366,
                baseline_payload=ORIGINAL, slot_frames=STATIC_MUTATION_FRAMES, role="mutation",
            )

    def test_blf_asc_mismatch_is_rejected(self):
        frames = episode_messages(MUTATED)
        blf, asc = self.write_pair(frames, asc_frames=frames[:-1])
        with self.assertRaisesRegex(EpisodeValidationError, "frame counts differ"):
            validate_episode(
                blf, asc, phase_records(), channel=2, frame_id=0x366,
                baseline_payload=ORIGINAL, slot_frames=STATIC_MUTATION_FRAMES, role="mutation",
            )

    def test_preprobe_requires_three_bus_rx_and_stable_target_payload(self):
        frames = probe_messages()
        blf, asc = self.write_pair(frames, marker_phases=[probe_phase()])
        result = validate_probe(
            blf, asc, channel=2, frame_id=0x366,
            expected_payload=ORIGINAL, phase=probe_phase(),
        )
        self.assertEqual(result["baseline"]["payload_hex"], ORIGINAL.hex().upper())
        with self.assertRaisesRegex(EpisodeValidationError, "differs from campaign"):
            validate_probe(
                blf, asc, channel=2, frame_id=0x366,
                expected_payload=MUTATED, phase=probe_phase(),
            )

    def test_preprobe_rejects_any_canoe_tx(self):
        frames = probe_messages()
        frames.append(message(4, 0, 0x111, b"\0" * 8, rx=False))
        blf, asc = self.write_pair(
            sorted(frames, key=lambda frame: frame.timestamp), marker_phases=[probe_phase()],
        )
        with self.assertRaisesRegex(EpisodeValidationError, "passive probe.*CANoe Tx"):
            validate_probe(
                blf, asc, channel=2, frame_id=0x366,
                expected_payload=ORIGINAL, phase=probe_phase(),
            )

    def test_preprobe_requires_exactly_one_matching_six_second_marker(self):
        frames = probe_messages()
        blf, asc = self.write_pair(frames, markers=False)
        with self.assertRaisesRegex(EpisodeValidationError, "exactly one CAPL phase window"):
            validate_probe(
                blf, asc, channel=2, frame_id=0x366,
                expected_payload=ORIGINAL, phase=probe_phase(),
            )
        blf, asc = self.write_pair(frames, marker_phases=[probe_phase()])
        shifted = {**probe_phase(), "start_ns": 200_000_000, "end_ns": 6_200_000_000}
        with self.assertRaisesRegex(EpisodeValidationError, "marker differs"):
            validate_probe(
                blf, asc, channel=2, frame_id=0x366,
                expected_payload=ORIGINAL, phase=shifted,
            )
        short = {**probe_phase(), "end_ns": 5_100_000_000}
        with self.assertRaisesRegex(EpisodeValidationError, "duration differs from 6s"):
            validate_probe(
                blf, asc, channel=2, frame_id=0x366,
                expected_payload=ORIGINAL, phase=short,
            )

    def test_baseline_phase_rejects_any_canoe_tx(self):
        frames = episode_messages(MUTATED)
        frames.append(message(10, 0, 0x111, b"\0" * 8, rx=False))
        with self.assertRaisesRegex(EpisodeValidationError, "baseline phase contains CANoe Tx"):
            self.validate_mutation(frames)

    def test_late_recovery_requires_source_target_rx(self):
        frames = [
            frame for frame in episode_messages(MUTATED)
            if not (frame.is_rx and frame.channel == 1
                    and frame.arbitration_id == 0x366
                    and 56.4 <= frame.timestamp - EPOCH < 61.4)
        ]
        with self.assertRaisesRegex(EpisodeValidationError, "recovery late window.*source target Rx"):
            self.validate_mutation(frames)

    def test_late_recovery_source_payload_must_be_original(self):
        frames = episode_messages(MUTATED)
        for frame in frames:
            if (frame.is_rx and frame.channel == 1 and frame.arbitration_id == 0x366
                    and 56.4 <= frame.timestamp - EPOCH < 61.4):
                frame.data = MUTATED
                break
        with self.assertRaisesRegex(EpisodeValidationError, "source target Rx differs"):
            self.validate_mutation(frames)

    def test_late_windows_require_all_three_bus_rx_coverage(self):
        frames = [
            frame for frame in episode_messages(MUTATED)
            if not (frame.is_rx and frame.channel == 2
                    and 35.2 <= frame.timestamp - EPOCH < 40.2)
        ]
        with self.assertRaisesRegex(EpisodeValidationError, "normal late window lacks CAN3 Rx"):
            self.validate_mutation(frames)

    def test_late_light_state_change_blocks_next_episode(self):
        frames = episode_messages(MUTATED)
        for frame in frames:
            if (frame.is_rx and frame.channel == 1 and frame.arbitration_id == 0x3D6
                    and 56.4 <= frame.timestamp - EPOCH < 61.4):
                frame.data = bytes.fromhex("0001000000000000")
        with self.assertRaisesRegex(EpisodeValidationError, "LH_Standlicht_H_aktiv"):
            self.validate_mutation(frames)

    def test_light_fault_only_is_recorded_as_advisory(self):
        frames = episode_messages(MUTATED)
        for frame in frames:
            if (frame.is_rx and frame.channel == 1 and frame.arbitration_id == 0x3D6
                    and 56.4 <= frame.timestamp - EPOCH < 61.4):
                frame.data = bytes.fromhex("8000000000000000")
        result = self.validate_mutation(frames)
        self.assertEqual(result["status"], "validated")
        self.assertTrue(any("LH_Aussenlicht_def" in item for item in result["advisory_observations"]))

    def test_missing_light_state_coverage_blocks_next_episode(self):
        frames = [
            frame for frame in episode_messages(MUTATED)
            if not (frame.is_rx and frame.arbitration_id == 0x3D6
                    and 35.2 <= frame.timestamp - EPOCH < 40.2)
        ]
        with self.assertRaisesRegex(EpisodeValidationError, "light-state.*coverage"):
            self.validate_mutation(frames)

    def test_late_watched_rate_change_blocks_next_episode(self):
        frames = [
            frame for frame in episode_messages(MUTATED)
            if not (frame.is_rx and frame.channel == 2
                    and frame.arbitration_id == 0x3CE
                    and 56.4 <= frame.timestamp - EPOCH < 59.0)
        ]
        with self.assertRaisesRegex(EpisodeValidationError, "late recovery rate changed"):
            self.validate_mutation(frames)

    def test_normal_tx_schedule_uses_measured_blf_timestamps(self):
        frames = episode_messages(MUTATED)
        normal_tx = [
            frame for frame in frames
            if not frame.is_rx and frame.arbitration_id == 0x366
            and 30.2 <= frame.timestamp - EPOCH < 40.2
        ]
        normal_tx[10].timestamp += .1
        with self.assertRaisesRegex(EpisodeValidationError, "normal target Tx has a non-50ms interval"):
            self.validate_mutation(frames)

    def test_slot_tx_schedule_uses_measured_blf_timestamps(self):
        frames = episode_messages(MUTATED)
        slot_tx = [
            frame for frame in frames
            if not frame.is_rx and frame.arbitration_id == 0x366
            and 40.3 <= frame.timestamp - EPOCH < 41.3
        ]
        slot_tx[-1].timestamp -= .2
        with self.assertRaisesRegex(EpisodeValidationError, "slot target Tx ends early"):
            self.validate_mutation(frames)

    def test_adjacent_phase_tx_uses_containing_phase_before_tolerance(self):
        phases = [
            {"label": "baseline", "start_s": .1, "end_s": 30.1},
            {"label": "normal", "start_s": 30.101, "end_s": 40.101},
        ]
        self.assertEqual(_phase_for_time(30.102, phases), "normal")
        self.assertEqual(_phase_for_time(30.101, phases), "normal")

    def test_resume_probe_matches_pre_exposure_normal(self):
        result = self.validate_resume()
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["reference_normal"]["source_target_rx"], 3)
        self.assertEqual(result["probe"]["late_source_target_rx"], 5)
        self.assertTrue(all(result["reference_normal"]["late_rx_counts"].values()))
        self.assertTrue(all(result["probe"]["late_rx_counts"].values()))

    def test_resume_probe_rejects_changed_source_payload_even_with_global_majority(self):
        frames = probe_messages()
        for frame in frames:
            if frame.is_rx and frame.channel == 1 and frame.arbitration_id == 0x366:
                frame.data = MUTATED
                break
        # The first source frame lies just outside the late five-second window.
        for frame in frames:
            if (frame.is_rx and frame.channel == 1 and frame.arbitration_id == 0x366
                    and frame.timestamp - EPOCH >= 2):
                frame.data = MUTATED
                break
        with self.assertRaisesRegex(EpisodeValidationError, "source target Rx differs"):
            self.validate_resume(probe_frames=frames)

    def test_resume_probe_rejects_missing_source_coverage(self):
        frames = [
            frame for frame in probe_messages()
            if not (frame.is_rx and frame.channel == 1 and frame.arbitration_id == 0x366
                    and frame.timestamp - EPOCH >= 4)
        ]
        with self.assertRaisesRegex(EpisodeValidationError, "fewer than three source"):
            self.validate_resume(probe_frames=frames)

    def test_resume_probe_rejects_watched_state_change(self):
        frames = probe_messages()
        for frame in frames:
            if frame.is_rx and frame.channel == 1 and frame.arbitration_id == 0x3D6:
                frame.data = bytes.fromhex("0001000000000000")
        with self.assertRaisesRegex(EpisodeValidationError, "LH_Standlicht_H_aktiv"):
            self.validate_resume(probe_frames=frames)

    def test_resume_probe_rejects_bcm02_rate_change(self):
        frames = episode_messages(MUTATED)
        frames.extend(
            message(35.3 + index * .2, 0, 0x2A0, b"\0" * 8)
            for index in range(20)
        )
        with self.assertRaisesRegex(EpisodeValidationError, "0x2A0 late recovery rate changed"):
            self.validate_resume(reference_frames=frames)

    def test_resume_probe_rejects_missing_light_coverage(self):
        frames = [
            frame for frame in probe_messages()
            if frame.arbitration_id != 0x3D6
        ]
        with self.assertRaisesRegex(EpisodeValidationError, "light-state coverage"):
            self.validate_resume(probe_frames=frames)

    def test_resume_probe_rejects_missing_bus_coverage(self):
        frames = [
            frame for frame in probe_messages()
            if not (frame.channel == 2 and frame.timestamp - EPOCH >= 1.1)
        ]
        with self.assertRaisesRegex(EpisodeValidationError, "resume probe late window lacks CAN3"):
            self.validate_resume(probe_frames=frames)

    def test_two_passive_probes_validate_new_stable_state_without_prestate_claim(self):
        result = self.validate_two_passive()
        self.assertEqual(result["status"], "validated")
        self.assertFalse(result["return_to_interrupted_prestate_confirmed"])
        self.assertEqual(result["reference"]["late_source_target_rx"], 5)
        self.assertEqual(result["candidate"]["late_source_target_rx"], 5)

    def test_two_passive_probes_reject_changed_door_state(self):
        frames = probe_messages()
        for frame in frames:
            if frame.channel == 0 and frame.arbitration_id == 0x583:
                frame.data = bytes.fromhex("0000000200000000")
        with self.assertRaisesRegex(EpisodeValidationError, "ZV_BT_offen"):
            self.validate_two_passive(candidate_frames=frames)

    def test_two_passive_probes_reject_changed_bcm02_rate(self):
        frames = probe_messages()
        frames.extend(
            message(1.3 + index * .2, 0, 0x2A0, b"\0" * 8)
            for index in range(20)
        )
        with self.assertRaisesRegex(EpisodeValidationError, "0x2A0"):
            self.validate_two_passive(reference_frames=frames)

    def test_two_passive_probes_reject_missing_late_source_rx(self):
        frames = [
            frame for frame in probe_messages()
            if not (frame.channel == 1 and frame.arbitration_id == 0x366
                    and frame.timestamp - EPOCH >= 4)
        ]
        with self.assertRaisesRegex(EpisodeValidationError, "fewer than three source"):
            self.validate_two_passive(candidate_frames=frames)

    def test_two_passive_probes_reject_identical_log_as_independent_evidence(self):
        blf, asc = self.write_pair(
            probe_messages(), marker_phases=[probe_phase()], stem="one_probe",
        )
        with self.assertRaisesRegex(EpisodeValidationError, "two distinct probes"):
            validate_passive_state_match(
                blf, asc, probe_phase(), blf, asc, probe_phase(),
                channel=2, frame_id=0x366, baseline_payload=ORIGINAL,
            )


if __name__ == "__main__":
    unittest.main()
