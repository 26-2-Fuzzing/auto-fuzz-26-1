"""Offline lifecycle checks for the CANoe paired capture driver."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import can

from canoe_paired_cycle import _capture_probe, build_parser, run


BASELINE = bytes.fromhex("00000000200000F0")


def write_baseline(path: Path) -> None:
    with can.BLFWriter(path) as writer:
        for index in range(3):
            writer.on_message_received(can.Message(
                timestamp=float(index + 1), channel=1, arbitration_id=0x366,
                data=BASELINE, is_rx=True, is_extended_id=False,
            ))


class FakeSchedule:
    phases: list[tuple[str, int]] = []
    schedules: list[tuple[str, tuple[tuple[float, bytes], ...]]] = []

    def __init__(self, application, channel, expected_config):
        self.sequence = 0
        self.clock_ns = 1_000_000_000

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        pass

    def run_phase(self, label, duration_seconds, frames, frame_id=0x366):
        self.sequence += 1
        start = self.clock_ns
        end = start + int(duration_seconds * 1e9)
        self.clock_ns = end + 100_000_000
        self.phases.append((label, len(frames)))
        self.schedules.append((label, tuple(frames)))
        return {
            "label": label, "request_seq": self.sequence, "result": 1,
            "submitted_frames": len(frames), "start_ns": start,
            "end_ns": end, "duration_seconds": duration_seconds,
        }


class PairedCycleTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reference = self.root / "reference.blf"
        write_baseline(self.reference)
        self.reference.with_suffix(".asc").write_bytes(b"Begin TriggerBlock\nEnd TriggerBlock\n")
        self.config = self.root / "ThreeChannelCAN.cfg"
        self.config.touch()
        self.campaign = self.root / "campaign"
        FakeSchedule.phases = []
        FakeSchedule.schedules = []
        self.captures = []
        self.capture_durations = []

    def args(self, execute=False, start=1, probe_only=False, exclude=None,
              door_open=False, method="signal_single", auto_reference=False):
        options = [
            "--channel", "2", "--canoe-config", str(self.config),
            "--campaign-dir", str(self.campaign),
            "--case-start", str(start), "-n", "1", "-m", method,
        ]
        if auto_reference:
            options.append("--capture-reference-baseline")
        else:
            options.extend(("--reference-baseline-log", str(self.reference)))
        if execute:
            options.append("-x")
        if probe_only:
            options.append("--probe-only")
        if exclude is not None:
            options.extend(("--exclude-interrupted-case", str(exclude)))
        if door_open:
            options.append("--accept-operator-door-open")
        return build_parser().parse_args(options)

    def capture(self, output_dir, duration_seconds, expected_config=None, action=None):
        self.assertEqual(expected_config, self.config)
        self.assertIn(duration_seconds, (0, 30.0))
        self.assertEqual(action is None, duration_seconds == 30.0)
        output_dir.mkdir(parents=True)
        if action is not None:
            action(object())
        blf = output_dir / "capture.blf"
        asc = output_dir / "capture.asc"
        shutil.copy2(self.reference, blf)
        shutil.copy2(self.reference.with_suffix(".asc"), asc)
        self.captures.append(output_dir)
        self.capture_durations.append(duration_seconds)
        return blf, asc

    def patched_dependencies(self):
        return (
            patch("canoe_paired_cycle.capture_measurement", side_effect=self.capture),
            patch("canoe_paired_cycle.CANoeScheduleInterface", FakeSchedule),
            patch("canoe_paired_cycle.validate_probe", return_value={
                "status": "validated",
                "baseline": {"payload_hex": BASELINE.hex().upper()},
            }),
            patch("canoe_paired_cycle.validate_episode", return_value={"status": "validated"}),
            patch("canoe_paired_cycle._disk_preflight"),
        )

    def test_preview_freezes_catalogue_without_measurement(self):
        with patch("canoe_paired_cycle.capture_measurement") as capture:
            run(self.args())
        capture.assert_not_called()
        manifest = json.loads((self.campaign / "campaign.json").read_text())
        self.assertEqual(manifest["workflow"], "canoe_paired_capture")
        self.assertEqual(manifest["total_available_cases"], 122)
        self.assertFalse((self.campaign / "case_0001").exists())

    def test_auto_reference_capture_then_probe_and_pair_reuse_same_logs(self):
        patches = self.patched_dependencies()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            run(self.args(probe_only=True, auto_reference=True))
            self.assertEqual(self.capture_durations, [30.0, 0])
            reference = self.campaign / "reference_baseline"
            marker = json.loads((reference / "reference.json").read_text())
            self.assertEqual(marker["baseline_payload_hex"], BASELINE.hex().upper())
            self.assertEqual(marker["matching_rx_frames"], 3)
            self.assertEqual(marker["blf_name"], "capture.blf")
            self.assertFalse((self.campaign / "case_0001").exists())

            run(self.args(execute=True, auto_reference=True))
            self.assertEqual(self.capture_durations, [30.0, 0, 0, 0, 0, 0])
            manifest = json.loads((self.campaign / "campaign.json").read_text())
            self.assertEqual(
                Path(manifest["reference_baseline_log"]), reference / "raw" / "capture.blf"
            )
            self.assertTrue((self.campaign / "case_0001" / "complete.json").is_file())

    def test_auto_reference_and_pair_run_in_one_invocation(self):
        patches = self.patched_dependencies()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            run(self.args(execute=True, auto_reference=True))
        self.assertEqual(self.capture_durations, [30.0, 0, 0, 0, 0])
        self.assertEqual(FakeSchedule.phases[0], ("probe", 0))
        self.assertTrue((self.campaign / "case_0001" / "complete.json").is_file())

    def test_incomplete_auto_reference_blocks_retry_before_send(self):
        def interrupted_capture(output_dir, duration_seconds, expected_config=None):
            output_dir.mkdir(parents=True)
            (output_dir / "partial.blf").write_bytes(b"incomplete")
            raise RuntimeError("baseline capture interrupted")

        with patch("canoe_paired_cycle.capture_measurement", side_effect=interrupted_capture), \
                patch("canoe_paired_cycle._disk_preflight"):
            with self.assertRaisesRegex(RuntimeError, "baseline capture interrupted"):
                run(self.args(execute=True, auto_reference=True))
        self.assertFalse((self.campaign / "case_0001").exists())
        with patch("canoe_paired_cycle.capture_measurement") as capture:
            with self.assertRaisesRegex(ValueError, "incomplete"):
                run(self.args(execute=True, auto_reference=True))
        capture.assert_not_called()

    def test_changed_auto_reference_log_blocks_retry_before_send(self):
        patches = self.patched_dependencies()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            run(self.args(auto_reference=True))
            blf = self.campaign / "reference_baseline" / "raw" / "capture.blf"
            with blf.open("ab") as output:
                output.write(b"changed")
            before = len(self.captures)
            with self.assertRaisesRegex(ValueError, "reference baseline log changed"):
                run(self.args(execute=True, auto_reference=True))
            self.assertEqual(len(self.captures), before)

    def test_auto_reference_requires_new_campaign_start_or_existing_reference(self):
        with patch("canoe_paired_cycle.capture_measurement") as capture:
            with self.assertRaisesRegex(ValueError, "must start at case 1"):
                run(self.args(start=2, auto_reference=True))
        capture.assert_not_called()

    def test_passive_probe_submits_no_can_frames(self):
        patches = self.patched_dependencies()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            run(self.args(probe_only=True))
        self.assertEqual(len(self.captures), 1)
        self.assertEqual(FakeSchedule.phases, [("probe", 0)])
        self.assertFalse((self.campaign / "case_0001").exists())

    def test_pair_has_two_full_episodes_and_replay_skips_completion(self):
        patches = self.patched_dependencies()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            run(self.args(execute=True))
            self.assertEqual(len(self.captures), 4)
            run(self.args(execute=True))
            self.assertEqual(len(self.captures), 4)
        pair = json.loads((self.campaign / "case_0001" / "complete.json").read_text())
        self.assertEqual(pair["status"], "captured")
        self.assertEqual({item["role"] for item in pair["episodes"]}, {"mutation", "noop"})
        self.assertEqual(
            FakeSchedule.phases,
            [("probe", 0), ("baseline", 0), ("normal", 200),
             ("slot", 20), ("recovery", 1)] * 2,
        )

    def test_other_static_family_runs_paired_without_temporal_reinterpretation(self):
        patches = self.patched_dependencies()
        with patches[0], patches[1], patches[2], patches[3] as validate, patches[4]:
            run(self.args(execute=True, method="undefined_enum"))
        pair = json.loads((self.campaign / "case_0001" / "complete.json").read_text())
        plan = pair["plan"]
        self.assertEqual(plan["method"], "undefined_enum")
        self.assertEqual(plan["slot_frame_count"], 20)
        self.assertEqual(plan["expected_can2_target_tx_per_pair"], 442)
        slots = [frames for label, frames in FakeSchedule.schedules if label == "slot"]
        by_role = dict(zip(plan["order"], slots))
        self.assertEqual(len(by_role["mutation"]), 20)
        self.assertEqual({payload for _, payload in by_role["mutation"]},
                         {bytes.fromhex(plan["mutation_payload_hex"])})
        self.assertEqual({payload for _, payload in by_role["noop"]}, {BASELINE})
        self.assertEqual([call.kwargs["slot_frames"] for call in validate.call_args_list], slots)

    def test_temporal_100ms_pair_schedules_sequence_and_matched_noop(self):
        patches = self.patched_dependencies()
        with patches[0], patches[1], patches[2], patches[3] as validate, patches[4]:
            run(self.args(execute=True, method="temporal_sequence"))
        pair = json.loads((self.campaign / "case_0001" / "complete.json").read_text())
        plan = pair["plan"]
        self.assertEqual(plan["method"], "temporal_sequence")
        self.assertEqual(plan["slot_frame_count"], 10)
        self.assertEqual(plan["expected_can2_target_tx_per_episode"], 211)
        self.assertEqual(plan["expected_can2_target_tx_per_pair"], 422)
        self.assertEqual([frame["offset_ms"] for frame in plan["mutation_slot_frames"]],
                         list(range(0, 1000, 100)))
        slots = [frames for label, frames in FakeSchedule.schedules if label == "slot"]
        by_role = dict(zip(plan["order"], slots))
        self.assertEqual([round(offset * 1000) for offset, _ in by_role["mutation"]],
                         list(range(0, 1000, 100)))
        self.assertEqual([payload.hex().upper() for _, payload in by_role["mutation"]],
                         [frame["payload_hex"] for frame in plan["mutation_slot_frames"]])
        self.assertGreater(len({payload for _, payload in by_role["mutation"]}), 1)
        self.assertEqual([round(offset * 1000) for offset, _ in by_role["noop"]],
                         list(range(0, 1000, 100)))
        self.assertEqual({payload for _, payload in by_role["noop"]}, {BASELINE})
        self.assertEqual([call.kwargs["slot_frames"] for call in validate.call_args_list], slots)

    def test_first_episode_validation_failure_prevents_second_episode(self):
        patches = self.patched_dependencies()
        with patches[0], patches[1], patches[2], patches[4]:
            with patch("canoe_paired_cycle.validate_episode", side_effect=ValueError("bad first capture")):
                with self.assertRaisesRegex(ValueError, "bad first capture"):
                    run(self.args(execute=True))
        self.assertEqual(len(self.captures), 2)
        self.assertTrue((self.campaign / "case_0001" / "error.json").is_file())
        self.assertFalse((self.campaign / "case_0001" / "complete.json").exists())

    def test_changed_completed_log_blocks_skip_and_next_case(self):
        patches = self.patched_dependencies()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            run(self.args(execute=True))
            with (self.captures[0] / "capture.blf").open("ab") as output:
                output.write(b"changed")
            with self.assertRaisesRegex(RuntimeError, "log changed"):
                run(self.args(execute=True))
            with self.assertRaisesRegex(RuntimeError, "log changed"):
                run(self.args(execute=True, start=2))
        self.assertEqual(len(self.captures), 4)

    def test_explicit_exclusion_uses_passive_probe_and_skips_failed_case(self):
        patches = self.patched_dependencies()
        audit = {"status": "audited_interruption", "partial_blf": str(self.reference),
                 "reference_normal_phase": {"label": "normal"}}
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch("canoe_paired_cycle._audit_interrupted_case", return_value=audit), \
                patch("canoe_paired_cycle.validate_resume_probe",
                      return_value={"status": "validated"}) as resume:
            run(self.args(execute=True))
            (self.campaign / "case_0002").mkdir()
            prior_captures = len(self.captures)
            run(self.args(exclude=2))
            self.assertEqual(len(self.captures), prior_captures + 1)
            self.assertEqual(FakeSchedule.phases[-1], ("probe", 0))
            resume.assert_called_once()
            excluded = json.loads((self.campaign / "case_0002" / "excluded.json").read_text())
            self.assertEqual(excluded["status"], "excluded")
            self.assertFalse((self.campaign / "case_0002" / "complete.json").exists())
            run(self.args(exclude=2))
            self.assertEqual(len(self.captures), prior_captures + 1)
            run(self.args(execute=True, start=3))
            self.assertTrue((self.campaign / "case_0003" / "complete.json").is_file())
            self.assertEqual(len(self.captures), prior_captures + 5)

    def test_changed_exclusion_marker_blocks_next_case_before_tx(self):
        patches = self.patched_dependencies()
        audit = {"status": "audited_interruption", "partial_blf": str(self.reference),
                 "reference_normal_phase": {"label": "normal"}}
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch("canoe_paired_cycle._audit_interrupted_case", return_value=audit), \
                patch("canoe_paired_cycle.validate_resume_probe",
                      return_value={"status": "validated"}):
            run(self.args(execute=True))
            (self.campaign / "case_0002").mkdir()
            run(self.args(exclude=2))
            marker = self.campaign / "case_0002" / "excluded.json"
            changed = json.loads(marker.read_text())
            changed["resume_validation"]["status"] = "unverified"
            marker.write_text(json.dumps(changed))
            prior_captures = len(self.captures)
            with self.assertRaisesRegex(RuntimeError, "exclusion marker"):
                run(self.args(execute=True, start=3))
            self.assertEqual(len(self.captures), prior_captures)

    def test_failed_passive_state_check_does_not_exclude_or_send(self):
        patches = self.patched_dependencies()
        audit = {"status": "audited_interruption", "partial_blf": str(self.reference),
                 "reference_normal_phase": {"label": "normal"}}
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch("canoe_paired_cycle._audit_interrupted_case", return_value=audit), \
                patch("canoe_paired_cycle.validate_resume_probe",
                      side_effect=ValueError("late state changed")):
            run(self.args(execute=True))
            (self.campaign / "case_0002").mkdir()
            prior_captures = len(self.captures)
            with self.assertRaisesRegex(ValueError, "late state changed"):
                run(self.args(exclude=2))
            self.assertEqual(len(self.captures), prior_captures + 1)
            self.assertEqual(FakeSchedule.phases[-1], ("probe", 0))
            self.assertFalse((self.campaign / "case_0002" / "excluded.json").exists())
            with self.assertRaisesRegex(RuntimeError, "partial evidence"):
                run(self.args(execute=True, start=3))
            self.assertEqual(len(self.captures), prior_captures + 1)

    def test_partial_case_without_exclusion_blocks_later_case(self):
        patches = self.patched_dependencies()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            run(self.args(execute=True))
            (self.campaign / "case_0002").mkdir()
            prior_captures = len(self.captures)
            with self.assertRaisesRegex(RuntimeError, "partial evidence"):
                run(self.args(execute=True, start=3))
            self.assertEqual(len(self.captures), prior_captures)

    def test_operator_door_open_exclusion_matches_two_passive_probes(self):
        patches = self.patched_dependencies()
        audit = {"status": "audited_interruption", "partial_blf": str(self.reference),
                 "reference_normal_phase": {"label": "normal"}}
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch("canoe_paired_cycle._audit_interrupted_case", return_value=audit), \
                patch("canoe_paired_cycle.validate_passive_state_match",
                      return_value={"status": "validated",
                                    "return_to_interrupted_prestate_confirmed": False}) as compare:
            run(self.args(execute=True))
            case_dir = self.campaign / "case_0002"
            case_dir.mkdir()
            previous_dir = case_dir / "exclusion_checks" / "20261125T085126365193Z"
            _capture_probe(previous_dir, self.config, 2, 0x366, BASELINE)
            captures_before = len(self.captures)
            run(self.args(exclude=2, door_open=True))
            compare.assert_called_once()
            self.assertEqual(len(self.captures), captures_before + 1)
            self.assertEqual(FakeSchedule.phases[-1], ("probe", 0))
            marker = json.loads((case_dir / "excluded.json").read_text())
            self.assertEqual(marker["mode"], "operator_reported_state_transition")
            self.assertIs(marker["operator_reported_door_open"], True)
            self.assertIs(marker["pre_exposure_state_match"], False)
            self.assertFalse((case_dir / "complete.json").exists())
            run(self.args(execute=True, start=3))
            self.assertTrue((self.campaign / "case_0003" / "complete.json").is_file())

    def test_operator_door_open_comparison_failure_keeps_case_partial(self):
        patches = self.patched_dependencies()
        audit = {"status": "audited_interruption", "partial_blf": str(self.reference),
                 "reference_normal_phase": {"label": "normal"}}
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch("canoe_paired_cycle._audit_interrupted_case", return_value=audit), \
                patch("canoe_paired_cycle.validate_passive_state_match",
                      side_effect=ValueError("new state drifted")):
            run(self.args(execute=True))
            case_dir = self.campaign / "case_0002"
            case_dir.mkdir()
            previous_dir = case_dir / "exclusion_checks" / "20261125T085126365193Z"
            _capture_probe(previous_dir, self.config, 2, 0x366, BASELINE)
            with self.assertRaisesRegex(ValueError, "new state drifted"):
                run(self.args(exclude=2, door_open=True))
            self.assertFalse((case_dir / "excluded.json").exists())
            with self.assertRaisesRegex(RuntimeError, "partial evidence"):
                run(self.args(execute=True, start=3))

    def test_operator_door_open_retry_uses_latest_validated_probe(self):
        patches = self.patched_dependencies()
        audit = {"status": "audited_interruption", "partial_blf": str(self.reference),
                 "reference_normal_phase": {"label": "normal"}}
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch("canoe_paired_cycle._audit_interrupted_case", return_value=audit), \
                patch("canoe_paired_cycle.validate_passive_state_match", side_effect=[
                    ValueError("new state drifted"),
                    {"status": "validated", "return_to_interrupted_prestate_confirmed": False},
                ]) as compare:
            run(self.args(execute=True))
            case_dir = self.campaign / "case_0002"
            case_dir.mkdir()
            old_dir = case_dir / "exclusion_checks" / "20000101T000000000000Z"
            _capture_probe(old_dir, self.config, 2, 0x366, BASELINE)
            phases_before = len(FakeSchedule.phases)
            with self.assertRaisesRegex(ValueError, "new state drifted"):
                run(self.args(exclude=2, door_open=True))
            operator_root = case_dir / "operator_state_checks"
            first_dir = next(operator_root.iterdir())
            first_probe = json.loads((first_dir / "probe.json").read_text())
            self.assertFalse((case_dir / "excluded.json").exists())
            # A later failed capture with no result must not replace the saved probe.
            (operator_root / "99999999T999999999999Z" / "probe" / "raw").mkdir(parents=True)

            run(self.args(exclude=2, door_open=True))
            self.assertEqual(FakeSchedule.phases[phases_before:], [("probe", 0)] * 2)
            self.assertEqual(compare.call_args_list[1].args[0], Path(first_probe["blf"]))
            self.assertNotEqual(compare.call_args_list[1].args[0],
                                Path(json.loads((old_dir / "probe.json").read_text())["blf"]))
            marker_path = case_dir / "excluded.json"
            marker = json.loads(marker_path.read_text())
            self.assertEqual(marker["new_state_reference_probe_root"],
                             "operator_state_checks")
            self.assertEqual(Path(marker["new_state_reference_probe_dir"]), first_dir)

            captures_before = len(self.captures)
            changed = dict(marker, new_state_reference_probe_root="exclusion_checks")
            marker_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(RuntimeError, "reference probe is outside"):
                run(self.args(execute=True, start=3))
            marker_path.write_text(json.dumps(marker))
            reference_log = Path(first_probe["blf"])
            original_log = reference_log.read_bytes()
            reference_log.write_bytes(original_log + b"changed")
            with self.assertRaisesRegex(RuntimeError, "Saved probe log changed"):
                run(self.args(execute=True, start=3))
            reference_log.write_bytes(original_log)
            self.assertEqual(len(self.captures), captures_before)
            run(self.args(execute=True, start=3))
            self.assertTrue((self.campaign / "case_0003" / "complete.json").is_file())

    def test_operator_door_open_requires_exclusion_action(self):
        with self.assertRaisesRegex(ValueError, "requires --exclude-interrupted-case 2"):
            run(self.args(door_open=True))


if __name__ == "__main__":
    import unittest

    unittest.main()
