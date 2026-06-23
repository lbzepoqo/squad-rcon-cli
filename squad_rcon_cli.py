"""Interactive RCON console for QA-testing Squad RCON updates.

Standalone — no external dependencies beyond Python 3.11+ stdlib.

Connects to one Squad server, sends whatever command you type, and prints the
raw reply. Because it never hard-codes the command set, it works for new or
changed RCON commands without any code change — which is the point for QA'ing a
stream of incoming RCON updates.

Two modes:
  - Interactive REPL (default): type a command, see the reply. Ctrl-D or "quit"
    to exit. Unsolicited server push messages (chat, events) are printed as
    they arrive, tagged [push].
  - One-shot (--command): run a single command, print the reply, exit. Useful
    for scripted/regression checks and piping.

Usage:
    python3 squad_rcon_cli.py --host <host> --port <port> --password <pw>
    python3 squad_rcon_cli.py --host <host> --port <port> --password <pw> --command "ListPlayers"

Examples:
    # Interactive session against a test server
    python3 squad_rcon_cli.py --host 1.2.3.4 --port 21114 --password secret

    # One-shot, good for a checklist or capturing output to a file
    python3 squad_rcon_cli.py --host 1.2.3.4 --port 21114 --password secret --command "ShowNextMap"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Callable

# ---------------------------------------------------------------------------
# Minimal Squad RCON protocol (UE4 RCON, not Valve Source RCON)
#
# Key differences from Valve's Source RCON:
#   - Packet type 0x01 (CHAT_VALUE) for unsolicited server push messages
#   - A 7-byte follow-response blob trails every empty end-of-response packet
#   - Multi-packet: send command + empty sentinel, collect until empty response
#
# This protocol layer is a proven implementation kept byte-for-byte stable so it
# matches Squad's actual wire format (verified against a live server).
# ---------------------------------------------------------------------------

FOLLOW_RESPONSE_BODY = b"\x00\x01\x00\x00\x00\x00\x00"
PACKET_HEADER_SIZE = 12  # size(4) + id(4) + type(4)


class PacketType(IntEnum):
    RESPONSE_VALUE = 0
    CHAT_VALUE = 1      # Squad-specific unsolicited push messages
    EXEC_COMMAND = 2
    AUTH_RESPONSE = 2   # Same wire value as EXEC_COMMAND; context determines which
    AUTH = 3


@dataclass
class RconPacket:
    packet_id: int
    packet_type: int
    body: str
    is_follow_response: bool = False


def encode_packet(packet_type: PacketType, packet_id: int, body: str) -> bytes:
    body_bytes = body.encode("utf-8")
    size = 4 + 4 + len(body_bytes) + 2  # id + type + body + two nulls
    return struct.pack(
        f"<iii{len(body_bytes)}scc",
        size, packet_id, int(packet_type),
        body_bytes, b"\x00", b"\x00",
    )


def decode_packet(data: bytes) -> tuple[RconPacket, int] | None:
    """Decode one packet from a byte buffer. Returns (packet, bytes_consumed) or None."""
    if len(data) < 4:
        return None
    size = struct.unpack_from("<i", data, 0)[0]
    total_length = size + 4
    if len(data) < total_length:
        return None

    packet_id = struct.unpack_from("<i", data, 4)[0]
    packet_type = struct.unpack_from("<i", data, 8)[0]
    body_length = size - 4 - 4 - 2
    body = data[PACKET_HEADER_SIZE : PACKET_HEADER_SIZE + body_length].decode("utf-8", errors="replace")

    is_follow = False
    consumed = total_length
    if (
        body == ""
        and packet_type == PacketType.RESPONSE_VALUE
        and len(data) >= total_length + len(FOLLOW_RESPONSE_BODY)
        and data[total_length : total_length + len(FOLLOW_RESPONSE_BODY)] == FOLLOW_RESPONSE_BODY
    ):
        is_follow = True
        consumed = total_length + len(FOLLOW_RESPONSE_BODY)

    return RconPacket(packet_id=packet_id, packet_type=packet_type, body=body, is_follow_response=is_follow), consumed


# ---------------------------------------------------------------------------
# Minimal async RCON client (no auto-reconnect — a QA session is short-lived;
# if the link drops we surface it and exit rather than hide it)
# ---------------------------------------------------------------------------

class RconError(Exception):
    pass

class RconConnectionError(RconError):
    pass

class RconAuthError(RconError):
    pass

class RconDisconnectedError(RconError):
    pass

class RconCommandTimeoutError(RconError):
    pass


@dataclass
class _PendingRequest:
    future: asyncio.Future[str]
    body_parts: list[str] = field(default_factory=list)


class RconClient:
    COMMAND_TIMEOUT = 10.0

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        on_chat: Callable[[str], None] | None = None,
        debug_bytes: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self._on_chat = on_chat
        self._debug_bytes = debug_bytes
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buffer = bytearray()
        self._pending: dict[int, _PendingRequest] = {}
        self._packet_id_counter = 0
        self._auth_request_id: int | None = None
        self._reader_task: asyncio.Task | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def _dump_bytes(self, direction: str, data: bytes) -> None:
        # Hex dump of raw wire traffic for protocol-level debugging. To stderr so
        # it stays out of the command-reply stream on stdout.
        if self._debug_bytes:
            print(f"[bytes {direction}] {len(data)}B {data.hex(' ')}", file=sys.stderr)

    def _next_id(self) -> int:
        self._packet_id_counter += 1
        if self._packet_id_counter >= 2**31:
            self._packet_id_counter = 1
        return self._packet_id_counter

    async def connect(self) -> None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.COMMAND_TIMEOUT,
            )
        except (OSError, asyncio.TimeoutError) as error:
            raise RconConnectionError(f"TCP connect failed: {error}") from error
        self._reader = reader
        self._writer = writer
        self._connected = True
        self._buffer.clear()
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._authenticate()

    async def _authenticate(self) -> None:
        assert self._writer is not None
        auth_id = self._next_id()
        auth_packet = encode_packet(PacketType.AUTH, auth_id, self.password)
        self._dump_bytes("->", auth_packet)  # note: contains the password
        self._writer.write(auth_packet)
        await self._writer.drain()
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending[auth_id] = _PendingRequest(future=future)
        self._auth_request_id = auth_id
        try:
            await asyncio.wait_for(future, timeout=self.COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(auth_id, None)
            self._auth_request_id = None
            raise RconAuthError("Authentication timed out")

    async def execute(self, command: str) -> str:
        if not self._connected or self._writer is None:
            raise RconDisconnectedError("Not connected")
        request_id = self._next_id()
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending[request_id] = _PendingRequest(future=future)
        outgoing = (
            encode_packet(PacketType.EXEC_COMMAND, request_id, command)
            + encode_packet(PacketType.EXEC_COMMAND, request_id, "")
        )
        self._dump_bytes("->", outgoing)
        self._writer.write(outgoing)
        await self._writer.drain()
        try:
            return await asyncio.wait_for(future, timeout=self.COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise RconCommandTimeoutError(f"Command timed out: {command}")

    async def close(self) -> None:
        self._connected = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while self._connected:
                data = await self._reader.read(8192)
                if not data:
                    self._on_disconnect()
                    return
                self._dump_bytes("<-", data)
                self._buffer.extend(data)
                self._process_buffer()
        except asyncio.CancelledError:
            return
        except Exception:
            self._on_disconnect()

    def _process_buffer(self) -> None:
        while self._buffer:
            result = decode_packet(bytes(self._buffer))
            if result is None:
                break
            packet, consumed = result
            del self._buffer[:consumed]
            if packet.packet_type == PacketType.CHAT_VALUE:
                if self._on_chat is not None and packet.body:
                    self._on_chat(packet.body)
            elif packet.packet_type == PacketType.AUTH_RESPONSE:
                self._handle_auth_response(packet)
            else:
                self._handle_response(packet)

    def _handle_auth_response(self, packet: RconPacket) -> None:
        if packet.packet_id == -1:
            if self._auth_request_id is None:
                return
            auth_id = self._auth_request_id
            self._auth_request_id = None
            pending = self._pending.pop(auth_id, None)
            if pending is not None and not pending.future.done():
                pending.future.set_exception(RconAuthError("Authentication failed"))
            return
        pending = self._pending.pop(packet.packet_id, None)
        if pending is not None:
            if packet.packet_id == self._auth_request_id:
                self._auth_request_id = None
            if not pending.future.done():
                pending.future.set_result("")

    def _handle_response(self, packet: RconPacket) -> None:
        if packet.is_follow_response:
            return
        pending = self._pending.get(packet.packet_id)
        if pending is None:
            return
        if packet.body == "":
            self._pending.pop(packet.packet_id)
            if not pending.future.done():
                pending.future.set_result("".join(pending.body_parts))
        else:
            pending.body_parts.append(packet.body)

    def _on_disconnect(self) -> None:
        self._connected = False
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(RconDisconnectedError("Connection lost"))
        self._pending.clear()
        self._auth_request_id = None


# ---------------------------------------------------------------------------
# REPL / one-shot front end
# ---------------------------------------------------------------------------

QUIT_WORDS = {"quit", "exit"}


class TranscriptLog:
    """Append-only NDJSON transcript: one timestamped {ts, dir, data} per line.

    A decoded record of the session for QA evidence (what was sent, what came
    back). Separate from --debug-bytes, which dumps raw wire bytes to stderr.
    """

    def __init__(self, path: str) -> None:
        self._handle = open(path, "a", encoding="utf-8")

    def record(self, direction: str, data: str) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "dir": direction,
            "data": data,
        }
        self._handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


async def run_one_shot(client: RconClient, command: str, transcript: TranscriptLog | None) -> int:
    if transcript:
        transcript.record("sent", command)
    response = await client.execute(command)
    if transcript:
        transcript.record("recv", response)
    print(response if response else "(empty response)")
    return 0


async def run_repl(client: RconClient, transcript: TranscriptLog | None) -> int:
    print("Connected. Type an RCON command and press Enter. Ctrl-D or 'quit' to exit.")
    while True:
        try:
            line = await asyncio.to_thread(input, "rcon> ")
        except EOFError:
            print()
            return 0
        command = line.strip()
        if not command:
            continue
        if command.lower() in QUIT_WORDS:
            return 0
        if transcript:
            transcript.record("sent", command)
        try:
            response = await client.execute(command)
        except RconDisconnectedError:
            if transcript:
                transcript.record("error", "connection lost")
            print("Connection lost — server closed the RCON session.", file=sys.stderr)
            return 1
        except RconCommandTimeoutError as error:
            if transcript:
                transcript.record("error", str(error))
            print(f"Timeout: {error}", file=sys.stderr)
            continue
        if transcript:
            transcript.record("recv", response)
        print(response if response else "(empty response)")


def _make_push_handler(transcript: TranscriptLog | None) -> Callable[[str], None]:
    def handler(body: str) -> None:
        # Unsolicited server messages (chat, events) arrive out of band; tag them
        # so they are not mistaken for a command's reply.
        print(f"\n[push] {body}")
        if transcript:
            transcript.record("push", body)
    return handler


async def amain(args: argparse.Namespace) -> int:
    transcript = TranscriptLog(args.log) if args.log else None
    client = RconClient(
        args.host,
        args.port,
        args.password,
        on_chat=_make_push_handler(transcript),
        debug_bytes=args.debug_bytes,
    )
    try:
        await client.connect()
    except RconConnectionError as error:
        print(f"Connect failed: {error}", file=sys.stderr)
        return 1
    except RconAuthError as error:
        print(f"Auth failed: {error}", file=sys.stderr)
        return 1
    try:
        if args.command is not None:
            return await run_one_shot(client, args.command, transcript)
        return await run_repl(client, transcript)
    finally:
        await client.close()
        if transcript:
            transcript.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive RCON console for QA-testing Squad RCON updates.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", required=True, help="Server IP or hostname")
    parser.add_argument("--port", required=True, type=int, help="RCON port (e.g. 21114)")
    parser.add_argument("--password", required=True, help="RCON password")
    parser.add_argument(
        "--command",
        help="Run a single command and exit (one-shot mode). Omit for an interactive session.",
    )
    parser.add_argument(
        "--log",
        metavar="FILE",
        help="Append a timestamped NDJSON transcript (sent/recv/push/error) to FILE.",
    )
    parser.add_argument(
        "--debug-bytes",
        action="store_true",
        help="Hex-dump raw wire traffic to stderr (protocol-level debugging; includes the auth packet).",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(amain(args)))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
