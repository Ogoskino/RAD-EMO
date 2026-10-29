# Dataset card

## Collection

RAD-EMO records individual adults performing scripted laboratory conditions that
vary object carriage, activity and prompted affect. The canonical collection has
15 participants and 114 recordings, covering 38 protocol conditions three times
each. There are 60 still and 54 walking recordings. Collection documentation
specifies harmless replica objects for knife trials.

The acquired modalities include RGB, millimetre wave radar, thermal imaging and
contact-pulse records. Availability is assessed per file and per processing
stage. The collection size does not imply 114 complete, readable, synchronised
multimodal recordings.

## Targets

Valence and arousal are self-reported on recorded 0–9 scales. The retained rule is

```text
L = (valence <= 4) and (arousal >= 6)
K = protocol-assigned knife carriage
joint_positive = K and L
joint_state = 1 + L + 2*K
```

| State | Knife carriage K | Reported LVHA L |
| --- | --- | --- |
| T1 | 0 | 0 |
| T2 | 0 | 1 |
| T3 | 1 | 0 |
| T4 | 1 | 1 |

Prompted emotion and reported affect are separate fields. Carriage position,
visibility and object presence describe the intended protocol unless an explicit
review field establishes adherence. The canonical metadata's legacy adherence
status is pending visual QC; that status must not be confused with the paper's
manually verified person boxes. Model outputs remain separate from labels.

## Observation support

Four central, nonoverlapping five-second video windows cover 5–25 seconds of each
recording. There are 456 candidate windows. Waveform availability checks exclude
17 clips, and AMPNet input support excludes one further clip, leaving the
common affect set of 438 clips from 113 recordings. The exact retained IDs and
input masks belong to the corresponding versioned archive manifest.

The joint study uses the 178 supported development clips with associated radar
inputs and 167 transfer clips. The original radar study evaluates 178 transfer
clips under its own window/support definition. These counts describe different
supports, not interchangeable test sets. See [paper subsets](PAPERS.md).

## Timing

Radar and camera observations are paired by recording and central window index.
Regular radar blocks contain 75 frames. Shorter or irregular acquisitions use six
consecutive frame-index blocks, retaining the central four. Paired blocks can
therefore have different durations at the two sensors.

Radar acquisition start times are reconstructed using logs or boot offsets
anchored using thermal end times plus 2.592 seconds. The processing uses assumed start uncertainties of 1, 5
or 15 seconds according to the available evidence, and an association tolerance
of 0.1 seconds around estimated timestamps. These are reconstruction assumptions;
the tolerance is not a measured synchronisation accuracy. No hardware-synchronised
ground truth is claimed.

The RGB branch uses facial observations nearest 128 target times as model input;
its predicted waveform is interpolated onto a 150-position grid at a nominal
30 Hz. The current studies do not establish waveform fidelity
through clock-aligned contact-pulse validation. Availability of a contact record
must not be presented as evidence of that validation.

## Data levels and access

Native ADC binaries, processed radar arrays, visual features, physiological
representations and prediction tables are different data levels. Every supplied
array needs its axes, units or unitless transformation, support masks and
processing provenance. Numeric features and pseudonymous IDs are not automatically
anonymous.

The preservation proposal separates public documentation, approved anonymised
files under the DMP embargo, and material requiring continued restricted access.
Only the documentation is staged for GitHub. RGB, thermal imagery, individual
reports, physiological traces, embeddings and per-person results are excluded
from this repository. The main Zenodo target includes approved recording-level
images and sensor measurements, with derived inputs as a companion. Access to
governed originals follows the NTU record. See [sensor-data scope](RAW_DATA.md).

## Scope and limitations

This is a small, scripted, single-laboratory collection. Affect is reported after
each recording and inherited by its clips. Clips within a recording and records
from the same person are correlated. Historical fitting and selection membership
must be preserved when interpreting results. Only three transfer participants
contribute joint-positive recordings, so pooled joint results do not establish
within-person discrimination for all six transfer participants.

The collection supports the reported laboratory recognition tasks. It does not
validate operational surveillance, harmful-intent prediction or clinical affect
assessment. The earlier, separately collected radar study is not part of this
15-participant collection.
