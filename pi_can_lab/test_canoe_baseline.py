"""Tests for selecting a source payload from completed CANoe recordings."""

import tempfile
import unittest
from pathlib import Path

import can

from canoe_baseline import select_baseline


class BaselineSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    @staticmethod
    def message(
        payload: bytes,
        *,
        channel: int = 1,
        can_id: int = 0x366,
        is_rx: bool = True,
        extended: bool = False,
        remote: bool = False,
    ) -> can.Message:
        return can.Message(
            timestamp=1_700_000_000.0,
            channel=channel,
            arbitration_id=can_id,
            data=payload,
            dlc=len(payload),
            is_rx=is_rx,
            is_extended_id=extended,
            is_remote_frame=remote,
        )

    def write(self, extension: str, messages: list[can.Message]) -> Path:
        path = self.root / f"capture{extension}"
        writer_type = can.ASCWriter if extension == ".asc" else can.BLFWriter
        with writer_type(path) as writer:
            for offset, message in enumerate(messages):
                message.timestamp += offset * 0.1
                writer.on_message_received(message)
        return path

    def test_selects_strict_majority_from_channel_two_rx_only(self):
        expected = bytes.fromhex("00000000200000F0")
        other = bytes.fromhex("00000000200000F1")
        messages = [
            self.message(expected),
            self.message(other),
            self.message(expected),
            self.message(expected),
            self.message(other, is_rx=False),
            self.message(other, channel=0),
            self.message(other, can_id=0x367),
            self.message(other, extended=True),
            self.message(b"", remote=True),
            self.message(b"\x01" * 7),
        ]
        for extension in (".asc", ".blf"):
            with self.subTest(extension=extension):
                path = self.write(extension, messages)
                result = select_baseline(path, 2, 0x366)
                self.assertEqual(result.payload, expected)
                self.assertEqual(result.matching_frames, 4)
                self.assertEqual(result.selected_frames, 3)
                self.assertEqual(result.to_record()["payload_hex"], expected.hex().upper())

    def test_rejects_sparse_or_unstable_source(self):
        first = bytes.fromhex("00000000200000F0")
        second = bytes.fromhex("00000000200000F1")
        for extension in (".asc", ".blf"):
            with self.subTest(extension=extension):
                sparse = self.write(extension, [self.message(first)] * 2)
                with self.assertRaisesRegex(ValueError, "at least 3 required"):
                    select_baseline(sparse, 2, 0x366)
                tied = self.write(
                    extension,
                    [self.message(first), self.message(second)] * 2,
                )
                with self.assertRaisesRegex(ValueError, "no strict majority"):
                    select_baseline(tied, 2, 0x366)

    def test_rejects_unfinished_asc(self):
        path = self.write(".asc", [self.message(b"\x00" * 8)] * 3)
        content = path.read_bytes()
        self.assertTrue(content.rstrip().endswith(b"End TriggerBlock"))
        path.write_bytes(content[:content.rfind(b"End TriggerBlock")])
        with self.assertRaisesRegex(ValueError, "no final End TriggerBlock"):
            select_baseline(path, 2, 0x366)

    def test_validates_channel_and_identifier(self):
        path = self.write(".blf", [self.message(b"\x00" * 8)] * 3)
        with self.assertRaisesRegex(ValueError, "positive CANoe channel"):
            select_baseline(path, 0, 0x366)
        with self.assertRaisesRegex(ValueError, "standard CAN ID"):
            select_baseline(path, 2, 0x1000)


if __name__ == "__main__":
    unittest.main()
