"""Choose a stable source payload from a completed CANoe CAN recording."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import can


@dataclass(frozen=True)
class BaselineSelection:
    log_path: Path
    channel: int
    can_id: int
    payload: bytes
    matching_frames: int
    selected_frames: int
    payload_counts: tuple[tuple[str, int], ...]

    @property
    def payload_hex(self) -> str:
        return self.payload.hex().upper()

    def to_record(self) -> dict:
        """Return JSON-compatible evidence for the chosen baseline."""
        return {
            "log_path": str(self.log_path),
            "channel": self.channel,
            "can_id": f"0x{self.can_id:X}",
            "payload_hex": self.payload_hex,
            "matching_frames": self.matching_frames,
            "selected_frames": self.selected_frames,
            "payload_counts": dict(self.payload_counts),
        }


def select_baseline(
    log_path: str | Path,
    channel: int,
    can_id: int,
    dlc: int = 8,
    minimum_frames: int = 3,
) -> BaselineSelection:
    """Read CANoe Rx frames and select a strict-majority payload.

    The channel argument is the one-based CANoe channel. python-can returns
    zero-based channel numbers for both CANoe ASC and BLF recordings.
    Call this only after the CANoe measurement and logging block have stopped.
    """
    if type(channel) is not int or channel < 1:
        raise ValueError("channel must be a positive CANoe channel number")
    if type(can_id) is not int or not 0 <= can_id <= 0x7FF:
        raise ValueError("can_id must be a standard CAN ID")
    if type(dlc) is not int or not 0 <= dlc <= 8:
        raise ValueError("dlc must be between 0 and 8 for Classic CAN")
    if type(minimum_frames) is not int or minimum_frames < 1:
        raise ValueError("minimum_frames must be positive")

    path = Path(log_path).resolve(strict=True)
    suffix = path.suffix.lower()
    readers = {".asc": can.ASCReader, ".blf": can.BLFReader}
    if suffix not in readers:
        raise ValueError("baseline log must be a CANoe .asc or .blf file")

    before = path.stat()
    if suffix == ".asc":
        with path.open("rb") as source:
            source.seek(max(0, before.st_size - 256))
            if not source.read().rstrip().endswith(b"End TriggerBlock"):
                raise ValueError("ASC recording has no final End TriggerBlock")

    counts: Counter[bytes] = Counter()
    with readers[suffix](path) as reader:
        if suffix == ".blf" and reader.file_size != before.st_size:
            raise ValueError("BLF declared size differs from the recorded file size")
        for message in reader:
            if (
                message.channel == channel - 1
                and message.arbitration_id == can_id
                and message.is_rx
                and not message.is_extended_id
                and not message.is_remote_frame
                and not message.is_error_frame
                and not message.is_fd
                and message.dlc == dlc
                and len(message.data) == dlc
            ):
                counts[bytes(message.data)] += 1

    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("CANoe recording changed while reading it")

    matching_frames = counts.total()
    if matching_frames < minimum_frames:
        raise ValueError(
            f"only {matching_frames} matching Rx frames; "
            f"at least {minimum_frames} required"
        )
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    payload, selected_frames = ranked[0]
    if selected_frames * 2 <= matching_frames:
        raise ValueError("target Rx payload has no strict majority")
    return BaselineSelection(
        log_path=path,
        channel=channel,
        can_id=can_id,
        payload=payload,
        matching_frames=matching_frames,
        selected_frames=selected_frames,
        payload_counts=tuple(
            (value.hex().upper(), count) for value, count in ranked
        ),
    )
