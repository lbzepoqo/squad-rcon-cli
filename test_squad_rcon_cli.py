"""Self-check for the RCON packet codec. Run: python3 test_squad_rcon_cli.py

No framework — plain asserts. Covers the wire-format quirks that have bitten us
before: the empty-packet follow-response blob, multi-packet buffering, and
reply chunks larger than the server's own inbound packet limit.
"""

import json
import os
import struct
import tempfile

import asyncio

from squad_rcon_cli import (
    FOLLOW_RESPONSE_BODY,
    MAX_PACKET_SIZE,
    PacketType,
    RconAuthError,
    RconClient,
    RconProtocolError,
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


def test_oversized_reply_chunk_decodes() -> None:
    # The server's 14..4096 limit applies only to packets it receives. Its own
    # chunks split at ~4096 characters, so a body of multibyte player names
    # exceeds 4096 bytes (4149 seen live). Rejecting those drops large
    # ListPlayers replies.
    body = "Игрок" * 900  # 4500 characters, 8100 bytes as UTF-8
    decoded, _ = _decode(encode_packet(PacketType.RESPONSE_VALUE, 5, body))
    assert decoded.body == body


def test_garbage_size_field_is_rejected() -> None:
    # Misframed text read as a size field lands far outside the plausible range.
    # Without the bound, a huge value parks the reader waiting for bytes that
    # never arrive and -4 makes the decoder consume nothing and spin forever.
    for size in (-4, 9, MAX_PACKET_SIZE + 1, 0x6D6F4361):
        packet = struct.pack("<iii", size, 1, 0) + b"\x00\x00"
        try:
            decode_packet(packet)
        except RconProtocolError:
            continue
        raise AssertionError(f"size {size} should have been rejected")


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


async def _fake_server(handle_packet):
    """Local server that decodes client packets and passes each one to handle_packet(writer, packet_id, type, body)."""

    async def on_client(reader, writer):
        buffer = b""
        while True:
            data = await reader.read(8192)
            if not data:
                return
            buffer += data
            while len(buffer) >= 4 and len(buffer) >= struct.unpack_from("<i", buffer, 0)[0] + 4:
                size = struct.unpack_from("<i", buffer, 0)[0] + 4
                packet_id, packet_type = struct.unpack_from("<ii", buffer, 4)
                body = buffer[12 : size - 2].decode("utf-8")
                buffer = buffer[size:]
                if await handle_packet(writer, packet_id, packet_type, body) == "close":
                    writer.close()
                    return

    return await asyncio.start_server(on_client, "127.0.0.1", 0)


def test_wrong_password_close_is_an_auth_error() -> None:
    # Squad v10.6 answers a wrong password with no packet: it closes the connection (verified live 2026-10-03).
    async def scenario():
        async def handle(writer, packet_id, packet_type, body):
            await asyncio.sleep(0.25)
            return "close"

        server = await _fake_server(handle)
        client = RconClient("127.0.0.1", server.sockets[0].getsockname()[1], "wrong")
        try:
            await client.connect()
        except RconAuthError as error:
            return str(error)
        finally:
            await client.close()
            server.close()
        return None

    message = asyncio.run(scenario())
    assert message is not None and "Authentication failed" in message, message


def test_concurrent_commands_answered_in_order() -> None:
    # The server answers concurrent commands in send order (verified live: 40 commands in one write).
    async def scenario():
        async def handle(writer, packet_id, packet_type, body):
            if packet_type == PacketType.AUTH:
                writer.write(encode_packet(PacketType.RESPONSE_VALUE, packet_id, ""))
                writer.write(encode_packet(PacketType.AUTH_RESPONSE, packet_id, ""))
            elif body:
                writer.write(encode_packet(PacketType.RESPONSE_VALUE, packet_id, f"reply to {body}"))
            else:
                end = encode_packet(PacketType.RESPONSE_VALUE, packet_id, "")
                writer.write(end + end + FOLLOW_RESPONSE_BODY)
            await writer.drain()

        server = await _fake_server(handle)
        client = RconClient("127.0.0.1", server.sockets[0].getsockname()[1], "x")
        try:
            await client.connect()
            return await asyncio.gather(*(client.execute(f"Command{index}") for index in range(40)))
        finally:
            await client.close()
            server.close()

    replies = asyncio.run(scenario())
    assert replies == [f"reply to Command{index}" for index in range(40)], replies[:3]


if __name__ == "__main__":
    test_encode_decode_roundtrip()
    test_unicode_body_survives()
    test_follow_response_blob_is_consumed()
    test_empty_response_waits_for_trailing_bytes()
    test_partial_packet_returns_none()
    test_multi_packet_buffer()
    test_oversized_reply_chunk_decodes()
    test_garbage_size_field_is_rejected()
    test_transcript_log_writes_ndjson()
    test_wrong_password_close_is_an_auth_error()
    test_concurrent_commands_answered_in_order()
    print("ok — all self-checks passed")
