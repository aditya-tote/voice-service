"""Private, capped, strictly local recording alignment metadata (not playback proof)."""

import json
import os

TIMELINE_LIMIT = 16 * 1024 * 1024
EVENTS = {"caller_arrival", "model_input", "synthetic_silence", "model_audio", "provider_send",
          "clear", "mark_matched", "output_suppressed", "output_discarded"}


class Timeline:
    def __init__(self, path, limit=TIMELINE_LIMIT):
        self.stream = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
        self.limit, self.size, self.truncated = limit, 0, False

    def write(self, event, offset_ms, samples=0, byte_offset=0, epoch=0):
        if event not in EVENTS:
            raise ValueError("invalid timeline event")
        values = (offset_ms, samples, byte_offset, epoch)
        if any(type(value) not in {int, float} or not 0 <= value <= 1e12 for value in values):
            raise ValueError("invalid timeline counter")
        row = json.dumps({"v": 1, "event": event, "offsetMs": round(offset_ms, 3),
                          "samples": samples, "byteOffset": byte_offset, "epoch": epoch},
                         separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if self.truncated or self.size + len(row) > self.limit:
            self.truncated = True
            return
        self.stream.write(row)
        self.size += len(row)

    def flush(self):
        self.stream.flush()

    def close(self):
        self.stream.close()
