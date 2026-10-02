# Offline CAN anomaly calibration

`replay_trial_analysis.py` replays completed capture files without transmitting
CAN frames, changing feedback state, or rewriting a trial. It is a diagnostic
tool, not an automatic mutation, verification, or exploitation path.
The stage-1 example profiles use a bounded 30-second passive baseline,
10-second original-payload normal replay, 1-second mutation slot, 20-second
recovery, and 50 ms send interval. Automatic feedback is disabled. These
durations are collection/safety limits, **not** statistical evidence that
low-frequency IDs are assessable in one second.

For a new ordered 0x366 exploration, use `experiment_runner.py --paired-cycle`
with the default 10-set invocation cap, then resume with the same
`--experiment-id`. This freezes the live baseline payload and a de-duplicated
eight-family catalogue; it does not repeat a promising candidate for
validation. The existing `--paired-sets N` mode remains available for
selector-driven independent pairs. In either mode, one set schedules a complete
mutation episode and a complete original-payload no-op episode. Their order
alternates across sets, and each episode
has its own passive baseline, normal replay, and recovery. The candidate
mutation and original payload are fixed before the first episode. The runner
checks that the first recovery returned to its pre-exposure state before it
starts the second; it also re-probes the exact original payload before each
injection. If a gate fails, the pair stays incomplete instead of being
silently retried or counted as a comparable control.
For a temporal mutation, the no-op repeats the original payload at the same
mutation-slot interval and frame-pattern length; actual TX timing is checked
before the pair can be called comparable.

The pair report separately checks pre-exposure state, complete RX/clock
coverage, actual TX payload and timing, and mutation-only versus no-op-shared
candidate events. A mutation-only event in a single pair is still
**unverified**. It should guide independent, state-matched replication and
external physical checks, never automatic feedback selection or exploitation.
Even a comparable pair cannot establish a low false-alert rate: repeated
independent held-out sets are needed.

## Run it

From `pi_can_lab`:

```sh
python3 replay_trial_analysis.py ../experiment_0001/experiment_0001 --trial 11
python3 replay_trial_analysis.py ../experiment_0001/experiment_0001 --trial 1,3-7 --passive-window-seconds 1 --passive-window-seconds 2
python3 replay_trial_analysis.py ../experiment_0001/experiment_0001 --kind no_op --output /tmp/can-noop-replay.json
```

The positional path can also be a wrapper containing exactly one
`experiment_*/experiment.json`. `--trial` accepts IDs and ranges and can be
repeated. `--kind` filters completed `mutation` or `no_op` trials. A no-op has
the same original and mutation-slot payload; its timing and send rate must
also match the mutation trial. `--dbc` overrides the experiment snapshot's
DBC. `--no-history` disables use of earlier completed trials as historical
controls. By default the report goes only to stdout. An explicit `--output`
creates a **new** file outside the experiment directory and refuses to
overwrite any existing file.

The report includes classified candidates and observations, inconclusive and
incomparable bus/ID comparison counts, no-op false-alert counts, phase lengths,
receiver clock alignment status/correction/uncertainty, and the preselected `0x2A0` state marker's phase/one-second
counts. The state marker is reported as evidence, not used to invent a state
label. The `tx.jsonl` record is also checked: a no-op is comparable only when
the completed sender contract, session and phase markers, target ID, original
payload, per-send timestamps, target interval, phase coverage, and sent counts
agree. An equal *average* rate is insufficient if one slot is bursty or only
partially transmitted. Missing or mismatched TX evidence is
reported separately as `unassessed` or `invalid`; those trials are excluded
from the comparable-no-op count. A no-op's candidate-like detector output is
counted as a false alert; it is never feedback eligible.

The clock report shows no numeric correction for invalid or uncertain
alignment. Older distributed captures with offset/RTT estimates but no shared
`reference_id` are treated as unaligned; offsets measured against different
controller clocks cannot be subtracted safely. An explicit mismatch between
metadata and mutation-file trial kinds fails replay instead of entering the
no-op false-alert denominator.

## Passive pseudo-phase negative controls

`--passive-window-seconds W` divides **only the recorded passive baseline**
into non-overlapping four-window groups of length `W`: reference baseline,
normal-like control, pseudo mutation, and pseudo recovery. Their order and timestamps are explicit in the
report. Real normal, mutation, and recovery frames are never relabeled clean.
For a 10-second baseline, 1-second windows yield two groups; 5-second
groups do not fit and are reported as unavailable. These windows have no
transmission difference and are useful for finding spontaneous bursts and
payload drift, but windows from one trial are dependent. Do not divide a false
alert count by the number of pseudo windows and call it an independent-trial
false-positive rate.

Pseudo windows use only **earlier completed trials** as history when ordinary
replay does; `--no-history` disables history for both. No later trial, nor any
mutation/recovery frame from the current trial, enters the clean pseudo windows.
They stress the detector against passive traffic; they are not a direct
estimate of live mutation-detection specificity. A separate no-op trial uses
the same original-payload send schedule in the mutation slot and measures
false alerts caused by transmission, phase changes, and ambient drift.

## Validation before setting thresholds

1. Use existing `experiment_0001` captures for development only. In those
   logs, `0x2A0` changed from tens of frames/second to 5 frames/second during
   `trial_0008` **before** the mutation slot, and `0x17330810` can burst to
   seven frames in a passive one-second window. `NavPos_01` (`0x486`) also has
   naturally changing GPS fields. None is a mutation-positive label by itself.
2. Exclude target-ID routing, DBC-defined dynamic/counter fields, and phase
   boundary uncertainty before scoring other IDs. Compare equal-duration
   windows from the same pre-intervention state. If state changes inside a
   control or there are too few frames, report `inconclusive` rather than a
   confident change. A longer fixed baseline cannot repair state drift.
3. Fix thresholds on development sessions using a trial-level score, including
   the maximum over all IDs, buses, and metrics if the live detector scans all
   of them. Keep later complete sessions/days held out; do not randomly split
   frames or overlapping windows across development and evaluation. Report
   trial-level `any false alert`, false alerts per capture hour, state-specific
   results, and the rate of abstentions. This tool does **not** tune thresholds
   or write any result back into the live runner.
4. For positive-path testing, use synthetic fixtures with a known non-target
   ID response to test report plumbing, then separately evaluate a controlled,
   independently labeled positive capture. Synthetic changed frames alone do
   not prove a physical response. Passive/no-op captures establish false-alert
   behavior, not mutation causality or recall.

With only ten completed baseline sessions, even zero observed trial-level
false alerts would give a one-sided 95% upper bound of about 26% on the true
false-alert probability (`1 - 0.05**(1/10)`). Small claimed false-positive
rates require many independent held-out sessions, not many correlated windows
from one session. Repeated mutation outcomes likewise need state-matched,
independently reset, randomized no-op comparisons; no fixed repeat count is
universally sufficient.

Run unit tests with `python3 -m unittest test_replay_trial_analysis -v`.
