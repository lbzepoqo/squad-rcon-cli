"""Self-check for the RCON packet codec. Run: python3 test_squad_rcon_cli.py

No framework — plain asserts. Covers the wire-format quirks that have bitten us
before: the empty-packet follow-response blob and multi-packet buffering.
"""

import json
import os
import tempfile

from squad_rcon_cli import (
    FOLLOW_RESPONSE_BODY,
    PacketType,
    TranscriptLog,
    decode_packet,
    encode_packet,
)


def _decode(data: bytes):
    result = decode_packet(data)
    assert result is not None, "expected a full packet"
    return result


def test_encode_decode_roundtrip() -> None:
    encoded = encode_packet(PacketType.EXEC_COMMAND, 7, "ListPlayers")
    decoded, consumed = _decode(encoded)
    assert consumed == len(encoded)
    assert decoded.packet_id == 7
    assert decoded.packet_type == PacketType.EXEC_COMMAND
    assert decoded.body == "ListPlayers"
    assert decoded.is_follow_response is False


def test_unicode_body_survives() -> None:
    # Player names can be non-ASCII; the body must round-trip as UTF-8.
    name = "Игрок｜日本語"
    decoded, _ = _decode(encode_packet(PacketType.RESPONSE_VALUE, 1, name))
    assert decoded.body == name


def test_follow_response_blob_is_consumed() -> None:
    # An empty RESPONSE_VALUE trailed by the 7-byte UE4 blob must be recognised
    # as a follow-response and consume the extra bytes, or the next decode
    # desyncs the whole stream.
    empty = encode_packet(PacketType.RESPONSE_VALUE, 3, "")
    decoded, consumed = _decode(empty + FOLLOW_RESPONSE_BODY)
    assert decoded.is_follow_response is True
    assert consumed == len(empty) + len(FOLLOW_RESPONSE_BODY)


def test_empty_response_waits_for_trailing_bytes() -> None:
    # An empty RESPONSE_VALUE packet is ambiguous until the next 7 bytes
    # arrive: consuming it early strands the follow-response blob at the
    # buffer head and permanently desyncs framing (seen live as an RCON
    # session going deaf on an open socket).
    empty = encode_packet(PacketType.RESPONSE_VALUE, 3, "")

    # No trailing bytes yet — wait, don't consume
    assert decode_packet(empty) is None

    # Partial blob — still wait
    assert decode_packet(empty + FOLLOW_RESPONSE_BODY[:3]) is None

    # Trailing bytes are the start of a real packet — consume as end marker
    next_packet = encode_packet(PacketType.RESPONSE_VALUE, 4, "next")
    decoded, consumed = _decode(empty + next_packet)
    assert decoded.is_follow_response is False
    assert consumed == len(empty)


def test_partial_packet_returns_none() -> None:
    # A response can span multiple TCP reads; an incomplete buffer must decode to
    # None (wait for more) rather than raise or return garbage.
    encoded = encode_packet(PacketType.RESPONSE_VALUE, 9, "partial")
    assert decode_packet(encoded[:-3]) is None


def test_multi_packet_buffer() -> None:
    # Two packets concatenated decode one at a time, consuming exactly their own
    # bytes so the second is still intact.
    first = encode_packet(PacketType.RESPONSE_VALUE, 1, "first")
    second = encode_packet(PacketType.RESPONSE_VALUE, 2, "second")
    decoded, consumed = _decode(first + second)
    assert decoded.body == "first"
    assert consumed == len(first)
    decoded2, _ = _decode((first + second)[consumed:])
    assert decoded2.body == "second"


def test_transcript_log_writes_ndjson() -> None:
    # Each record is one valid JSON line with ts/dir/data, and unicode names
    # (which Squad allows) survive round-trip.
    fd, path = tempfile.mkstemp(suffix=".ndjson")
    os.close(fd)
    try:
        log = TranscriptLog(path)
        log.record("sent", "AdminBan \"奥利奥\"")
        log.record("recv", "Success")
        log.close()
        lines = [json.loads(ln) for ln in open(path, encoding="utf-8").read().splitlines()]
        assert len(lines) == 2
        assert lines[0]["dir"] == "sent" and "奥利奥" in lines[0]["data"]
        assert lines[1]["dir"] == "recv" and lines[1]["data"] == "Success"
        assert all("ts" in line for line in lines)
    finally:
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    test_encode_decode_roundtrip()
    test_unicode_body_survives()
    test_follow_response_blob_is_consumed()
    test_empty_response_waits_for_trailing_bytes()
    test_partial_packet_returns_none()
    test_multi_packet_buffer()
    test_transcript_log_writes_ndjson()
    print("ok — all self-checks passed")
