#!/usr/bin/env python3
"""Export named Station tags and convert to a local LeRobotDataset. No motor commands."""
import argparse
import asyncio
import io
import json
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent


def resolve_tags(host, start_name, end_name):
    norma = Path(os.environ.get('NORMA_CORE_ROOT', ROOT / 'norma-core'))
    sys.path[:0] = [str(norma / 'software/station/shared'), str(norma)]
    from station_py import new_station_client
    from shared.gremlin_py import gremlin

    def decode(data):
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
            offset += value.size
            fields[tag.number] = value.value
        return dict(name=bytes(fields.get(12, b'')).decode(),
                    action=fields.get(10, 0),
                    pointer=int.from_bytes(fields.get(11, b''), 'little'),
                    time_ns=fields.get(2, 0))

    async def read():
        client = await new_station_client(host, logging.getLogger('station-tags'))
        stream = client.read_from_offset('inference-tags/rx', offset=b'\x00',
                                         limit=10001, step=1, buf_size=64)
        active, count = {}, 0
        while True:
            entry = await asyncio.wait_for(stream.data.get(), 15)
            if entry is None:
                break
            count += 1
            if count > 10000:
                raise ValueError('Tag history exceeds 10000 events; refusing an incomplete lookup')
            tag = decode(entry.Data)
            key = (tag['name'], tag['pointer'])
            if tag['action'] == 0:
                active[key] = tag
            else:
                active.pop(key, None)
        if stream.err:
            raise RuntimeError(str(stream.err))
        return list(active.values())

    tags = asyncio.run(asyncio.wait_for(read(), 45))

    def pick(name):
        matches = [t for t in tags if t['name'] == name]
        if not matches:
            matches = [t for t in tags if t['name'].casefold() == name.casefold()]
        if len(matches) != 1:
            available = ', '.join(f'{t["name"]}={t["pointer"]}' for t in tags)
            raise ValueError(f'Tag {name!r}: expected one match, found {len(matches)}. '
                             f'Available: {available}')
        return matches[0]

    start, end = pick(start_name), pick(end_name)
    if start['pointer'] >= end['pointer']:
        raise ValueError('Start tag must precede end tag')
    return start, end


def convert(source, destination, repo_id, fps, camera_names, provenance):
    import numpy as np
    import pyarrow.parquet as pq
    from PIL import Image
    from lerobot.datasets import LeRobotDataset

    rows = pq.read_table(source).to_pylist()
    if not rows:
        raise ValueError('Station export contains no frames')
    joint_count = len(rows[0]['joints'])
    image_count = len(rows[0]['images'])
    if joint_count != 8 or image_count != 2:
        raise ValueError(f'Expected eight joints and two cameras; got {joint_count}, {image_count}')
    names = camera_names or [f'camera_{i}' for i in range(image_count)]
    if len(names) != image_count or len(set(names)) != len(names):
        raise ValueError('Provide exactly two distinct camera names in exported image-list order')
    if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', name) for name in names):
        raise ValueError('Camera names must start with a letter and contain only letters, numbers or underscores')
    episodes = {}
    shapes = []
    for image in rows[0]['images']:
        with Image.open(io.BytesIO(image['jpeg'])) as frame:
            shapes.append(np.asarray(frame.convert('RGB')).shape)
    for row in rows:
        if len(row['joints']) != joint_count or len(row['images']) != image_count:
            raise ValueError('Joint or camera count changes within the export')
        episode = episodes.setdefault(row['episode_start_ns'], [])
        if episode and row['timestamp_ns_since_episode_start'] <= episode[-1]['timestamp_ns_since_episode_start']:
            raise ValueError('Source timestamps do not increase within an episode')
        episode.append(row)
    slots = [f'joint_slot_{i}' for i in range(joint_count)]
    features = {
        'observation.state': dict(dtype='float32', shape=(joint_count,), names=slots),
        'action': dict(dtype='float32', shape=(joint_count,), names=slots),
        'source.timestamp': dict(dtype='float32', shape=(1,), names=['seconds']),
        'source.timestamp_ns': dict(dtype='int64', shape=(1,), names=['nanoseconds']),
    }
    for name, shape in zip(names, shapes):
        features[f'observation.images.{name}'] = dict(dtype='image', shape=shape,
                                                     names=['height', 'width', 'channels'])
    dataset = LeRobotDataset.create(repo_id, fps=fps, features=features, root=destination,
                                   robot_type='elrobot_station', use_videos=False,
                                   image_writer_threads=2)
    try:
        for episode in episodes.values():
            for row in episode:
                frame = {
                    'observation.state': np.asarray([j['position_norm'] for j in row['joints']], dtype=np.float32),
                    'action': np.asarray([j['goal_norm'] for j in row['joints']], dtype=np.float32),
                    'source.timestamp': np.asarray([row['timestamp_ns_since_episode_start'] / 1e9], dtype=np.float32),
                    'source.timestamp_ns': np.asarray([row['timestamp_ns_since_episode_start']], dtype=np.int64),
                    'task': row['task'],
                }
                for index, name in enumerate(names):
                    with Image.open(io.BytesIO(row['images'][index]['jpeg'])) as image:
                        pixels = np.asarray(image.convert('RGB'))
                    if pixels.shape != shapes[index]:
                        raise ValueError(f'Camera {name} resolution changes within the export')
                    frame[f'observation.images.{name}'] = pixels
                dataset.add_frame(frame)
            dataset.save_episode()
    finally:
        dataset.finalize()
    notes = dict(source=str(source), repo_id=repo_id, fps=fps, camera_names=names,
                 camera_mapping_user_supplied=bool(camera_names), uploaded=False,
                 joint_mapping='Slots preserve Station export order; physical motor IDs are not in the source schema.',
                 timing='Uniform viewer timeline; original episode-relative timestamps preserved in source.timestamp_ns.',
                 state='position_norm', action='goal_norm', **provenance)
    (destination / 'conversion_notes.json').write_text(json.dumps(notes, indent=2) + '\n')
    loaded = LeRobotDataset(repo_id, root=destination)
    if len(loaded) != len(rows) or loaded.num_episodes != len(episodes):
        raise RuntimeError('Reloaded dataset counts differ from the Station export')
    loaded[0]  # Confirm images and vectors decode through the public reader.
    print(f'Verified {len(loaded)} frames, {loaded.num_episodes} episodes: {destination}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True, help='New local dataset name; existing output is never overwritten')
    parser.add_argument('--start-tag')
    parser.add_argument('--end-tag')
    parser.add_argument('--source', type=Path, help='Existing merged Station Parquet; skips Station/tag lookup')
    parser.add_argument('--host', default=os.environ.get('STATION_HOST', 'localhost'))
    parser.add_argument('--task', default=os.environ.get('TASK', 'place pepperoni on pizza'), help='Task label for a new Station export')
    parser.add_argument('--episode-duration', type=int, default=int(os.environ.get('EPISODE_DURATION', '45')))
    parser.add_argument('--fps', type=int, default=10)
    parser.add_argument('--camera-names', help='Verified camera names in image-list order, e.g. front,top')
    parser.add_argument('--dry-run', action='store_true', help='Resolve tags and print paths; do not export, convert or launch')
    parser.add_argument('--view', action='store_true', help='Opt in to starting the LeRobot/Rerun web viewer')
    parser.add_argument('--web-port', type=int, default=9090)
    parser.add_argument('--grpc-port', type=int, default=9876)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', args.name):
        parser.error('--name must contain only letters, digits, underscores and hyphens')
    if args.fps <= 0 or args.episode_duration <= 0:
        parser.error('FPS and episode duration must be positive')
    if args.source and (args.start_tag or args.end_tag):
        parser.error('Use either --source or a start/end tag pair')
    if not args.source and not (args.start_tag and args.end_tag):
        parser.error('Provide --source or both --start-tag and --end-tag')
    destination = REPO / 'data' / f'lerobot-{args.name}'
    prefix = REPO / 'data' / f'{args.name}-station'
    source = args.source.resolve() if args.source else prefix.with_suffix('.parquet')
    repo_id = f'local/{args.name}'
    provenance = {}
    if not args.source:
        start, end = resolve_tags(args.host, args.start_tag, args.end_tag)
        provenance = dict(start_tag=start, end_tag=end)
        print(f'Tag range: {start["name"]} ({start["pointer"]}) -> {end["name"]} ({end["pointer"]})')
    print(f'Source: {source}\nDestination: {destination}')
    if args.dry_run:
        return
    if destination.exists() or (not args.source and (source.exists() or prefix.exists())):
        parser.error('Output already exists. Choose a new --name; no files were overwritten.')
    if not args.source:
        generator = Path(os.environ.get('NORMA_CORE_ROOT', ROOT / 'norma-core')) / 'bin/dataset-generator'
        source.parent.mkdir(parents=True, exist_ok=True)
        with source.with_suffix('.export.log').open('w') as log:
            subprocess.run([str(generator), '-no-tui', '-robot', args.host,
                            '-from', str(start['pointer']), '-to', str(end['pointer']),
                            '-episode.duration', str(args.episode_duration), '-task', args.task,
                            '-output', str(prefix)], stdout=log, stderr=subprocess.STDOUT, check=True)
    if not source.is_file():
        parser.error(f'No merged Station Parquet at {source}; inspect the export log for rejected episodes')
    convert(source, destination, repo_id, args.fps,
            args.camera_names.split(',') if args.camera_names else None, provenance)
    viewer = [str(Path(sys.executable).parent / 'lerobot-dataset-viz'), '--repo-id', repo_id,
              '--root', str(destination), '--episode-index', '0', '--mode', 'distant',
              '--web-port', str(args.web_port), '--grpc-port', str(args.grpc_port)]
    print('View later:\n' + shlex.join(viewer))
    if args.view:
        subprocess.run(viewer, check=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, asyncio.TimeoutError, subprocess.CalledProcessError) as error:
        sys.exit(f'Conversion failed: {error}')
