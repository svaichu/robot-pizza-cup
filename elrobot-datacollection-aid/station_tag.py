#!/usr/bin/env python3
"""Mark live Station recording boundaries; never issue motor commands."""
import argparse
import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
NORMA = Path(os.environ.get('NORMA_CORE_ROOT', ROOT / 'norma-core'))
sys.path[:0] = [str(NORMA / 'software/station/shared'), str(NORMA)]
from station_py import new_station_client, send_commands
from shared.gremlin_py import gremlin
from target.gen_python.protobuf.station import commands, drivers, inference

STATE = ROOT.parent / 'data' / 'station-tag-session.json'


def run_action(action, host):
    subprocess.run([sys.executable, '-u', str(Path(__file__).resolve()), action, '--host', host], check=True)


def listen(host, active):
    """Keep reading stdin; e closes a take, pauses five seconds, then opens another."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    terminal_settings = termios.tcgetattr(fd) if os.isatty(fd) else None
    recording = False

    def pause():
        print('Next take starts in 5 seconds. Press q to stop.', flush=True)
        deadline = time.monotonic() + 5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            ready, _, _ = select.select([fd], [], [], remaining)
            if ready:
                key = os.read(fd, 1)
                if not key or key.lower() == b'q' or key == b'\x03':
                    return False
                # Other input during the reset interval does not queue another take.

    try:
        if terminal_settings is not None:
            tty.setcbreak(fd)
        if active:
            print(f'Resuming active take: {active["name"]}', flush=True)
        else:
            run_action('start', host)
        recording = True
        print('Listening to stdin: e = end, wait 5s, start next; q or Ctrl+C = finish. No Enter needed.', flush=True)
        while True:
            key = os.read(fd, 1)
            if not key or key.lower() == b'q' or key == b'\x03':
                break
            if key.lower() != b'e':
                continue
            run_action('end', host)
            recording = False
            if not pause():
                break
            run_action('start', host)
            recording = True
            print('Recording. Press e for the next take or q to finish.', flush=True)
    except KeyboardInterrupt:
        print('\nFinishing recording.', flush=True)
    except subprocess.CalledProcessError:
        # The child reports whether acknowledgement was uncertain; do not retry a write.
        recording = False
        raise
    finally:
        if terminal_settings is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, terminal_settings)
        if recording:
            run_action('end', host)
        print('Stopped listening.', flush=True)


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def tag_event(data):
    reader, offset, fields = gremlin.Reader(data), 0, {}
    while offset < len(data):
        tag = reader.read_tag_at(offset)
        offset += tag.size
        if tag.number in (1, 2, 3, 10):
            value = reader.read_uint64(offset)
        elif tag.number in (11, 12):
            value = reader.read_bytes_view(offset)
        else:
            offset = reader.skip_data(offset, tag.wire)
            continue
        fields[tag.number] = value.value
        offset += value.size
    return fields


async def mark(host, names, dry_run):
    client = await new_station_client(host, logging.getLogger('station-tag'))
    try:
        latest = client.read_from_tail('inference-states', offset=b'\x00', limit=1, step=1, buf_size=1)
        entry = await asyncio.wait_for(latest.data.get(), 8)
        if entry is None:
            raise RuntimeError(f'No inference state available: {latest.err}')
        stamp = inference.InferenceRxReader(memoryview(bytes(entry.Data))).get_local_stamp_ns()
        age = (time.time_ns() - stamp) / 1e9
        if not -3 <= age <= 3:
            raise RuntimeError(f'Latest inference state is not current (age {age:.1f}s); refusing tag')
        pointer = int.from_bytes(bytes(entry.ID.ID), 'little')
        print(f'{"Would tag" if dry_run else "Tagging"}: {", ".join(names)}, pointer={pointer}, age={age:.2f}s', flush=True)
        if dry_run:
            return pointer
        # inference_tags.Command: type=CT_ADD_TAG (default 0), pointer=2, tag=3.
        # Match the web UI's eight-byte little-endian inference pointer.
        ptr = pointer.to_bytes(8, 'little')
        pending = {name.encode() for name in names}
        batch = []
        for encoded_name in (name.encode() for name in names):
            body = b'\x12' + varint(len(ptr)) + ptr + b'\x1a' + varint(len(encoded_name)) + encoded_name
            batch.append(commands.DriverCommand(type=drivers.StationCommandType.STC_INFERENCE_TAG_COMMAND, body=body))
        # A newly requested follow stream may not be subscribed before enqueue
        # completes. Anchor at an existing event, then poll finite saved history
        # so confirmation also sees tags written during that subscription race.
        snapshot = client.read_from_tail('inference-tags/rx', offset=b'\x00', limit=1, step=1, buf_size=2)
        previous = await asyncio.wait_for(snapshot.data.get(), 8)
        cursor = bytes(previous.ID.ID) if previous is not None else b'\x00'
        await send_commands(client, batch)

        async def confirm():
            nonlocal cursor
            while pending:
                history = client.read_from_offset('inference-tags/rx', offset=cursor, limit=256, step=1, buf_size=64)
                while True:
                    saved = await history.data.get()
                    if saved is None:
                        break
                    cursor = bytes(saved.ID.ID)
                    fields = tag_event(saved.Data)
                    if (fields.get(10, 0) == 0 and bytes(fields.get(12, b'')) in pending
                            and int.from_bytes(fields.get(11, b''), 'little') == pointer):
                        pending.remove(bytes(fields[12]))
                if history.err:
                    raise RuntimeError(f'Cannot read saved tag confirmation: {history.err}')
                if pending:
                    await asyncio.sleep(0.1)

        try:
            await asyncio.wait_for(confirm(), 8)
        except (asyncio.TimeoutError, RuntimeError) as error:
            raise RuntimeError('Tag batch was sent but not fully confirmed. Check Station tags before retrying.') from error
        print('All tags confirmed saved in Station.', flush=True)
        return pointer
    finally:
        client.close_connection()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='interactive', choices=['interactive', 'start', 'end', 'e', 'status'],
                        help='Default: keep listening to stdin; e ends a take, waits 5s, and starts another')
    parser.add_argument('--name', help='Optional recording base name for start')
    parser.add_argument('--host', default=os.environ.get('STATION_HOST', 'localhost'))
    parser.add_argument('--dry-run', action='store_true', help='Read the current pointer without writing tags or session state')
    args = parser.parse_args()
    active = json.loads(STATE.read_text()) if STATE.exists() else None
    if args.action == 'status':
        print(json.dumps(active, indent=2) if active else 'No active recording tag pair.')
        return
    if args.name and args.action != 'start':
        parser.error('--name is only valid with start')
    if active and active['host'] != args.host:
        parser.error(f'Active pair belongs to host {active["host"]}')
    if args.action == 'interactive':
        if args.dry_run:
            parser.error('Use start --dry-run for a read-only connection check')
        listen(args.host, active)
        return
    if args.action == 'e' and not args.dry_run:
        if not active:
            parser.error('No active pair. Run start first.')
        run_action('end', args.host)
        print('Waiting 5 seconds before the next take.', flush=True)
        time.sleep(5)
        run_action('start', args.host)
        return
    if args.action == 'start':
        if active:
            parser.error(f'Already active: {active["name"]}. End it with end first.')
        name = args.name or datetime.now().strftime('cheese_take_%Y%m%d_%H%M%S_%f')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', name):
            parser.error('Name must contain only letters, digits, underscores and hyphens')
        tag = name + '_start'
        tags = [tag]
    else:
        if not active:
            parser.error('No active pair. Run start first.')
        name, tag = active['name'], active['name'] + '_end'
        tags = [tag]
        if args.action == 'e':
            print('Dry run previews the end pointer only; a new take would start 5 seconds later.')
    pointer = asyncio.run(asyncio.wait_for(mark(args.host, tags, args.dry_run), 25))
    if args.dry_run:
        return
    if args.action == 'start':
        STATE.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATE.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(name=name, host=args.host, start_tag=tag, start_pointer=pointer,
                                             task=os.environ.get('TASK', 'place cheese on pizza')), indent=2) + '\n')
        temporary.replace(STATE)
    else:
        STATE.unlink()
    if args.action in ('e', 'end'):
        print(f'Completed range: {active["start_tag"]} ({active["start_pointer"]}) -> {active["name"]}_end ({pointer})')
    if args.action == 'start':
        print(f'Active take: {name}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, asyncio.TimeoutError, subprocess.CalledProcessError) as error:
        sys.exit(f'Tagging failed: {error}')
