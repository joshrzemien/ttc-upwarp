# Tick Tock Clock Upwarp Investigation

This repository contains the evidence, tooling, raw search output, and bounded technical analysis of [DOTA_Teabag's 2013 Super Mario 64 Tick Tock Clock upwarp](https://www.youtube.com/watch?v=bhBf5crp0i8).

**Verdict:** the observed teleport is reproduced by Mario's 32-bit Y coordinate changing from `0xC5837800` (`-4207.0`) to `0xC4837800` (`-1051.75`). That clears `0x01000000`, divides Y by four, and raises Mario by exactly `3155.25` units. The surviving evidence does not identify what caused that state change.

**This is not a bounty claim.** No normal-gameplay trigger or start-to-finish `.m64` was found.

![Incident comparison timeline](comparison_timeline.jpg)

## Result

| Property | Reconstructed value |
| --- | ---: |
| Pre-event Y | `-4207.0` (`0xC5837800`) |
| Post-event Y | `-1051.75` (`0xC4837800`) |
| XOR | `0x01000000` |
| Instantaneous rise | `3155.25` units |
| Source action | `ACT_FREEFALL` (`0x0100088C`) |
| TTC speed | Stopped |
| Recomputed floor / ceiling | `-2487.0` / `-173.0` |
| Upper landing | Y `-2487.0`, 24 game updates after the first post-mutation physics step |

The injected mutation matched both the instrumented N64 emulator and Wafel on the first physics step: Y `-1071.75`, vertical velocity `-24.0`, floor `-2487.0`, and ceiling `-173.0`. The complete fall then landed on the upper platform with matching timing.

The no-mutation control remained on the lower route and landed at Y `-5211.0`.

## What was checked

### Matched game source

The Japanese decompilation was built byte-identically to the tested ROM. The audit covered each relevant Mario Y-writer family:

- normal airborne integration and floor collision;
- ledge grabs and ceiling hangs;
- ground-pound startup;
- moving and retained platforms;
- object, pole, cannon, whirlpool, and attachment snaps;
- warps, spawns, cutscenes, and area initialization;
- raw coordinate writes observed during native tracing.

The incident's `ACT_FREEFALL` path cannot invoke the ceiling-hang snap. Ledge placement is bounded to 160 units above the query, ground-pound startup adds at most 20 units, and the stopped TTC spinner contributes exactly zero vertical platform displacement. The retained-platform analysis found no reachable stale-slot reuse path.

See [`results/software_mechanism_bounds.json`](results/software_mechanism_bounds.json) and [`results/platform_reachability.json`](results/platform_reachability.json).

### Bounded native search

The Wafel harness evaluated `484,122,309` clean native frames, including `218,045,574` at the incident's stopped-clock setting. A candidate required a one-frame rise of at least 500 units; none was found.

| Campaign | Coverage | Candidates | Largest rise |
| --- | ---: | ---: | ---: |
| Broad one-frame states, all TTC speeds | 354,633,684 frames | 0 | `426.764404` |
| Exact reconstructed stopped-clock state | 100,000,000 frames | 0 | `20.0` |
| Integer X/Z grid, 16 passes | 5,931,089 valid frames | 0 | `25.0004883` |
| Sixteen-frame incident-envelope episodes | 23,557,536 frames | 0 | `136.40625` |

The known bit-clear mutation was included as a positive control and was detected with a `3135.25`-unit rise after the first physics step.

See [`results/wafel_search_summary.json`](results/wafel_search_summary.json) and the raw logs under [`results/wafel_fuzz/`](results/wafel_fuzz/).

## Interpretation and limits

The analysis resolves the state transition and resulting trajectory with high confidence. It does **not** establish the physical initiator.

A transient in RDRAM, CPU/cache/register state, a bus, power, or EMI is the best fit to a one-bit value change, but is not directly evidenced. An ionizing particle is one possible transient source; the popular "cosmic ray" explanation is therefore possible, not proven. An undiscovered software memory-corruption write or deliberate edit remains logically possible, but neither is supported by the source audit, traces, replays, platform proof, bounded searches, or continuous race footage.

The randomized campaigns are bounded searches rather than an exhaustive proof over every 32-bit game state. No original input movie, savestate, RAM dump, or console hardware survives from the incident.

## Repository map

| Path | Contents |
| --- | --- |
| [`results/analysis_summary.json`](results/analysis_summary.json) | Canonical machine-readable conclusion and aggregate evidence |
| [`results/reproduction.json`](results/reproduction.json) | Injected emulator trajectory and no-flip control |
| [`results/incident_state_envelope.json`](results/incident_state_envelope.json) | State and geometry reconstructed from the surviving video and TAS corpus |
| [`results/software_mechanism_bounds.json`](results/software_mechanism_bounds.json) | Source-level Mario Y-writer inventory and bounds |
| [`results/platform_reachability.json`](results/platform_reachability.json) | Retained-platform and spinner-slot reachability proof |
| [`results/platform_corpus/`](results/platform_corpus/) | Extracted platform-state traces from the reference TAS attempts |
| [`results/wafel_fuzz/`](results/wafel_fuzz/) | Raw native campaign logs and per-campaign summaries |
| [`scripts/wafel_incident_fuzz.c`](scripts/wafel_incident_fuzz.c) | Direct-frame Wafel search harness |
| [`scripts/run_wafel_campaign.sh`](scripts/run_wafel_campaign.sh) | Parallel campaign runner |
| [`scripts/summarize_wafel_campaign.py`](scripts/summarize_wafel_campaign.py) | Raw-log summarizer |
| [`scripts/run_reproduction.py`](scripts/run_reproduction.py) | Emulator injection/control runner and assertions |
| [`scripts/reproduce_bitflip.lua`](scripts/reproduce_bitflip.lua) | Fuzzy64 coordinate-mutation and trace hook |
| [`fuzzy64-mario-y-trace.patch`](fuzzy64-mario-y-trace.patch) | Native MIPS Mario Y-write instrumentation |
| [`results/sources.json`](results/sources.json) | Source manifest, revisions, media, and citations |

## Re-running the artifacts

This repository intentionally excludes copyrighted ROM data, compiled third-party projects, and savestates. You need a legally obtained Japanese Super Mario 64 ROM with MD5:

```text
85d61f5525af708c9f1e84dce6dc10e9
```

The recorded environment used:

- [`n64decomp/sm64`](https://github.com/n64decomp/sm64) at `9921382a68bb0c865e5e45eb594d9c64db59b1af`;
- [`branpk/wafel`](https://github.com/branpk/wafel) at `5b808b60af15d316a5e2b0f87db34421d6225b57`;
- [`danebou/Fuzzy64`](https://github.com/danebou/Fuzzy64);
- [`danebou/TTC-Upwarp-Overlay`](https://github.com/danebou/TTC-Upwarp-Overlay);
- MinGW-w64 and Wine for the Windows `libsm64` harness.

The scripts preserve absolute paths from the research workstation and are archival rather than turnkey packaging. Update `ROOT`, emulator paths, the Wine path, and the Wafel DLL path for your environment before running them.

After supplying those dependencies, the emulator differential reproduction is:

```bash
python3 scripts/run_reproduction.py --output /tmp/reproduction.json
diff -u results/reproduction.json /tmp/reproduction.json
```

The Wafel harness interface is:

```text
wafel_incident_fuzz.exe DLL SEED TRIALS WARMUP SPEED_MODE HORIZON RESET_MODE PROFILE
```

Profiles are `0` broad, `1` exact incident state, `2` local incident envelope, `3` integer X/Z grid, and `4` bit-clear positive control. `RESET_MODE` is `0` for a continuous evolving world or `1` for snapshot restoration.

Existing raw campaign logs can be summarized without the emulator dependencies:

```bash
python3 scripts/summarize_wafel_campaign.py \
  results/wafel_fuzz/single_frame_400m \
  --output /tmp/single_frame_400m_summary.json
```

## Primary references

- [Original upwarp highlight](https://www.youtube.com/watch?v=bhBf5crp0i8)
- [Full race VOD](https://www.youtube.com/watch?v=TTh3LY-5KKg)
- [Original bounty announcement and terms](https://www.youtube.com/watch?v=aNzTUdOHm9A)
- [Ceiling-warp versus C5-to-C4 comparison](https://www.youtube.com/watch?v=X5cwuYFUUAY)
- [Complete source manifest](results/sources.json)
