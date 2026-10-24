# Convert Station recordings to LeRobotDataset

Use this when given start/end tag names and a new dataset name. It reads saved Station history, exports Station Parquet, converts it into a local LeRobotDataset, and reloads the result to verify frame/episode counts and image decoding. It does not command the robot, recalibrate hardware, or upload to Hugging Face.

**The viewer does not launch by default.** Add `--view` only when explicitly requested.

## Continuous stdin recording helper

Station continuously logs; the helper marks boundaries in that history. As of 2026-10-24, the chosen export task in `elrobot.env` is `place cheese on pizza`. Task text is assigned on export, not stored in a Station boundary tag.

With Station already running, launch this in your own WSL terminal:

```bash
cd /home/nachi/code/robot-pizza-cup/elrobot
bash scripts/station_tag.sh
```

The helper starts the first take immediately (or resumes the locally tracked active take), chooses unique `cheese_take_DATE_TIME_start/end` names, and keeps reading stdin. Press **e** without Enter to save the current end tag, wait **five seconds** for resetting the scene, and save a new start tag at the current Station pointer. The five-second reset is outside both tagged take ranges. The process continues listening for the next **e**. Press **q**, **Ctrl+C**, or close stdin to end the final take and exit; **q** also cancels a pending next take during the five-second pause. Additional e presses during that pause are ignored. Terminal settings are restored on exit.

The optional one-shot commands still work: `start` begins one take, `end` finishes it, and `e` finishes it then starts another after five seconds. Interactive mode is the default with no arguments; `interactive` explicitly selects it. Starting interactive mode creates a real start tag, so run it only when ready to collect.

The helper saves the active pair in `data/station-tag-session.json`. Use `bash scripts/station_tag.sh status` to recover the active name. `--name NAME` can override the name on a one-shot `start`. `start --dry-run` reads the current pointer without writing tags or session state. Every write waits for confirmation in Station's tag queue. No motor commands are sent. Running the helper locally avoids chat latency; tags use the current inference-state pointer when the command executes.

Use the exact generated start/end names with `station_dataset.sh` for later export. The older `cheesepos1`, `cheesepos2` and `cheesepos3` ranges were recorded before this helper; their full names include Station UI timestamp prefixes.

## Export named tags and convert

### Preserve each tagged take as exactly one episode

For the stdin helper's short takes, use `scripts/export_tagged_takes.py` with a saved tag-pair inventory. It reads Station's saved `inference/normvla` frames directly, keeps frames whose global inference-state IDs fall in each half-open `[start, end)` range, and gives every selected pair its own episode. It applies no movement detector or minimum-command filter. It verifies eight joints and two cameras, recovers camera ordering from every frame's source inference snapshot, and normalizes the images to a consistent camera queue order. Source timestamps are preserved, and the manifest reports frame gaps rather than silently discarding affected takes.

Example for the completed 2026-10-24 cheese takes, excluding the 6.04-second and 1.18-second ranges:

```bash
../so101/.venv/bin/python scripts/export_tagged_takes.py \
  --inventory ../data/cheese-takes-inventory-20261024-012900.json \
  --output ../data/cheese-takes-20261024-station.parquet \
  --exclude-name cheese_take_20261024_012637_278283 \
  --exclude-name cheese_take_20261024_012805_334407 \
  --task 'place cheese on pizza'

bash scripts/station_dataset.sh \
  --source ../data/cheese-takes-20261024-station.parquet \
  --name cheese-takes-20261024
```

The exporter also writes a sibling `.manifest.json` and `.previews/` directory. Inspect both camera previews before supplying logical names with `--camera-names`. The output path must be new. Converting this Parquet preserves its episode groups; the viewer still requires an explicit `--view`. Use the regular `--start-tag/--end-tag` path below when Station's automatic episode detector is wanted instead.

Station must already be running and have the requested tags/history available.

```bash
cd /home/nachi/code/robot-pizza-cup/elrobot
bash scripts/station_dataset.sh \
  --start-tag Test1 --end-tag Tag2Stop \
  --name pizza-test-02
```

`STATION_HOST`, `TASK` and `EPISODE_DURATION` are read from `elrobot.env`. Override them with `--host`, `--task` and `--episode-duration` if needed. Task labels are assigned during a new export; an existing source's task labels are preserved during offline conversion.

The command resolves active tag add/remove events. It prefers exact name matches and otherwise allows case-insensitive matching. Missing or ambiguous tags cause an error listing available names; it never guesses a different endpoint. Reused names at different pointers are ambiguous. The start pointer must precede the end pointer.

Inspect tag resolution without exporting or changing files:

```bash
bash scripts/station_dataset.sh \
  --start-tag Test1 --end-tag Tag2Stop \
  --name pizza-test-02 --dry-run
```

Use a new name for each export: existing datasets or intermediate outputs are never overwritten.

## Output paths

For `--name pizza-test-02`:

```text
robot-pizza-cup/data/
  pizza-test-02-station.parquet      # original Station export
  pizza-test-02-station.export.log   # kept/discarded episode diagnostics
  lerobot-pizza-test-02/             # converted dataset root
    meta/
    data/
    conversion_notes.json
```

The pinned Station release temporarily writes numbered episode files, then merges them into `OUTPUT.parquet` and removes the episode directory. This command uses the final merged file. The old directory-only `milestone4.sh` validator is not the recommended export path for this release.

If no merged Parquet is produced, inspect the export log for empty or rejected episodes. Do not silently relax duration or command-count thresholds. Partial output from a failed conversion is retained for diagnosis; choose a fresh name when retrying.

## Convert an existing Station Parquet offline

Station does not need to be running:

```bash
bash scripts/station_dataset.sh \
  --source ../data/test1-12707083-12791062.parquet \
  --name test1-offline
```

The standalone Python command is also available if `elrobot.env` is absent:

```bash
../so101/.venv/bin/python scripts/station_dataset.py \
  --source ../data/test1-12707083-12791062.parquet --name test1-offline
```

For the hackathon's small test recordings, conversion reads the source into memory. Large recordings will need a streaming conversion rather than this helper.

## Data mapping and limitations

- Each source `episode_start_ns` group becomes one LeRobot episode; the command reports counts rather than assuming there are three episodes.
- Eight `position_norm` values become `observation.state`.
- Eight `goal_norm` values become `action`.
- Two JPEG streams become image-backed LeRobot features; no video encoding is required for the viewer.
- Camera names default to `camera_0` and `camera_1`. They preserve the source image-list order; Station's changing camera numbers are not copied into the names.
- Joint dimensions are named `joint_slot_0` through `joint_slot_7`. The source schema lacks motor IDs. Physical joint order must be verified before using the result for control or training.
- Viewer timing is a uniform **10 Hz** by default (`--fps` changes it). No source frames are dropped or interpolated. Original timestamps remain in `source.timestamp_ns` (integer nanoseconds) and `source.timestamp` (seconds). The uniform timeline can differ from actual elapsed time.
- Structural checks and successful visualization do not certify grasp success, calibration, exact sensor synchronization, or training readiness.
- Provenance, resolved tags, task source and conversion choices are saved in `conversion_notes.json`.

Once camera image-list order is verified, supply explicit names. For example, **only if index 0 is wrist and index 1 is overhead**:

```bash
bash scripts/station_dataset.sh \
  --source ../data/test1-12707083-12791062.parquet \
  --name test1-named --camera-names front,top
```

If the order is reversed, use `--camera-names top,front`. Do not infer order from earlier Station identifiers 19/18.

## View only when requested

To load every episode into one browser viewer as separate local Rerun recordings:

```bash
../so101/.venv/bin/python -u scripts/view_all_episodes.py \
  --repo-id local/cheese-takes-20261024 \
  --root ../data/lerobot-cheese-takes-20261024 \
  --web-port 9090 --grpc-port 9876
```

The helper writes one `.rrd` file per episode into a new timestamped sibling folder, plus `sources.json` with recording IDs and episode mapping. `--output-dir` can select a new folder explicitly. Each file has a unique recording ID, an episode name, and its own timeline starting at zero. All files are opened together by the Rerun CLI web viewer. Select a recording from Rerun's source list; the label shows current and original episode indices. Episode timelines are not concatenated. Stop another viewer using the same ports before running this command. The CLI prints its browser URL when the files are loaded.

To convert and launch the LeRobot/Rerun viewer:

```bash
bash scripts/station_dataset.sh \
  --start-tag Test1 --end-tag Tag2Stop \
  --name pizza-test-03 --view
```

The command stays running while serving the viewer. Default ports are 9090 (web) and 9876 (Rerun gRPC). Use different `--web-port` and `--grpc-port` values if another viewer is already running. Open:

http://localhost:9090/?url=rerun%2Bhttp%3A%2F%2Flocalhost%3A9876%2Fproxy

Without `--view`, the command exits after conversion and prints the viewer command to run later. For an existing converted dataset:

```bash
../so101/.venv/bin/lerobot-dataset-viz \
  --repo-id local/pizza-test-02 \
  --root /home/nachi/code/robot-pizza-cup/data/lerobot-pizza-test-02 \
  --episode-index 0 --mode distant --web-port 9090 --grpc-port 9876
```

Change `--episode-index` to review other episodes.

## Procedure for future requests

When the user supplies tags:

1. Resolve the exact active tag pair with `--dry-run`; report missing/ambiguous names rather than substituting.
2. Export/convert using a fresh meaningful `--name`.
3. Read the export log and converted frame/episode counts.
4. Provide the output path. Launch the viewer only if explicitly requested.
5. Record any verified camera mapping; preserve unverified mappings as numbered streams.

## Existing Test1 recording

The available pair was `Test1` (12707083) to `Tag2Stop` (12791062), not an `endtest1` tag. The original conversion is `data/lerobot-test1/`. It contains one episode, 405 frames, eight state/action values and two image streams. The new reusable command was checked separately against these real tags using a fresh output name, without launching another viewer.
