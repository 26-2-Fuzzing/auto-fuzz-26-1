"""File-only adapters for CANoe logs and explicitly chosen DBC signals."""

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    timestamp: float
    channel: int
    arbitration_id: int
    data: bytes
    is_rx: bool | None = True
    is_extended: bool = False
    is_error: bool = False
    is_remote: bool = False
    is_fd: bool = False


def read_log(path):
    suffix = path.suffix.lower()
    if suffix not in {".asc", ".blf"}:
        raise ValueError("expected a .blf or .asc file")
    import can

    options = {"relative_timestamp": True} if suffix == ".asc" else {}
    try:
        with can.LogReader(path, **options) as log:
            for m in log:
                yield Frame(m.timestamp, m.channel, m.arbitration_id, bytes(m.data),
                            m.is_rx, m.is_extended_id, m.is_error_frame, m.is_remote_frame, m.is_fd)
    except can.CanError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def dbc_decoder(path, watches, known_buses):
    import cantools

    db = cantools.database.load_file(path)
    by_frame = defaultdict(list)
    seen = set()
    for watch in watches:
        bus, message_name, signal = watch["bus"], watch["message"], watch["signal"]
        if bus not in known_buses:
            raise ValueError(f"watch {message_name}.{signal}: unknown bus {bus}")
        msg = db.get_message_by_name(message_name)
        if signal not in {s.name for s in msg.signals}:
            raise ValueError(f"watch {message_name}.{signal}: signal not found in DBC")
        key = (bus, msg.frame_id, msg.is_extended_frame)
        if (key, signal) in seen:
            raise ValueError(f"duplicate watch: {message_name}.{signal}")
        seen.add((key, signal))
        by_frame[key].append((msg, signal))

    def decode(bus, frame):
        result = {}
        cache = {}
        for msg, signal in by_frame.get((bus, frame.arbitration_id, frame.is_extended), ()):
            if msg.name not in cache:
                try:
                    cache[msg.name] = msg.decode(frame.data, decode_choices=False)
                except cantools.database.DecodeError as exc:
                    raise ValueError(str(exc)) from exc
            if signal in cache[msg.name]:
                result[f"{msg.name}.{signal}"] = cache[msg.name][signal]
        return result

    return decode
