# RAD-EMO

**Multimodal laboratory recordings of object carriage and reported affect**

RAD-EMO brings together RGB images, thermal images, millimetre-wave radar
measurements and contact-pulse acquisition records from **114 recordings with
15 participants**. Its **38 protocol conditions**, each recorded three times,
vary object carriage, still or walking activity, placement and prompted affect.
Participants report valence, arousal and dominance after each recording.

The collection supports research on carried-object recognition, reported affect,
contactless sensing and multimodal analysis. Recording identifiers connect the
sensor files, acquisition metadata, annotations and labels.

**Zenodo:** [10.5281/zenodo.23765400](https://doi.org/10.5281/zenodo.23765400)
is reserved; the dataset deposit is in preparation. This repository provides
documentation, illustrations, aggregate metadata and verification utilities.

![Conceptual overview of RAD-EMO, showing RGB, thermal, radar and pulse channels linked at recording level.](assets/rad_emo_dataset_overview.png)

*Conceptual illustration of the collection; the depicted scene and sensor outputs
are schematic.*

## Collection at a glance

| Item | Current collection inventory |
| --- | --- |
| Participants / recordings | 15 / 114 |
| Protocol conditions | 38, each recorded three times |
| Activity | 60 still and 54 walking recordings |
| RGB | 189,835 images across 114 recordings; 640 × 480 pixels |
| RGB timing | Variable saved cadence, approximately 30–36 images/s; use recorded frame timestamps |
| Thermal | 65,367 images across 114 recordings, with frame/time tables |
| Native radar | ADC binaries and indexes for 90 recordings from 12 participants |
| Radar system | 77 GHz TI cascade MIMO; 12 transmitters, 16 receivers; nominal 15 frames/s |
| Contact pulse | Acquisition fields recorded alongside RGB frame timestamps |
| Reported affect | Valence, arousal and dominance on recorded 0–9 scales |

Counts describe the inventoried collection. Modality availability and observation
support are recorded separately; see [sensor-data coverage](RAW_DATA.md).

## Recording conditions and labels

![Conceptual guide to RAD-EMO object-carriage, activity and placement categories, and the valence, arousal and dominance scales.](assets/rad_emo_recording_conditions.png)

*Recording categories and reported-affect scales.*

Object conditions comprise no target object, phone, harmless replica knife,
and phone plus replica knife. The protocol also varies still/walking activity
and open/concealed placement. These are recording-protocol fields; prompted
affect and subsequent self-reports are retained separately.

The declared low-valence/high-arousal label is
`valence <= 4 and arousal >= 6`. A joint label combines this report with
protocol-assigned knife carriage. These labels describe the recorded protocol
attributes and participants' affect reports. Field definitions and
the categorical T1–T4 mapping are in the [dataset card](DATASET_CARD.md) and
[data dictionary](DATA_DICTIONARY.md).

## Sensor files and temporal alignment

The main dataset scope includes image sequences and native radar measurements,
together with acquisition records, annotation exports, labels and timing
provenance. Native radar files retain the four devices' ADC/index correspondence
and acquisition settings. Existing complex ADC MAT exports form an optional
processed representation; derived features and saved analysis inputs form a
separate companion layer.

RGB, thermal and radar streams are associated by recording and temporally aligned
using recorded camera/thermal timestamps, authentic radar relative timing and
documented cross-sensor offsets. Alignment supports approximate session/window
analysis, with reconstruction methods and assigned uncertainties retained in the
timing tables. Native sampling and recorded gaps are preserved.

See [images and sensor recordings](RAW_DATA.md) for coverage and
[directory structure](docs/DIRECTORY_STRUCTURE.md) for the repository layout
and proposed recording-level archive paths.

## Explore the repository

| Resource | Contents |
| --- | --- |
| [Dataset card](DATASET_CARD.md) | Collection, targets, observation support and timing |
| [Directory structure](docs/DIRECTORY_STRUCTURE.md) | Files in this repository and proposed sensor archive organisation |
| [Data dictionary](DATA_DICTIONARY.md) | Recording, clip, file and array field definitions |
| [Collection summary](metadata/collection_summary.json) | Machine-readable collection counts and sampling metadata |
| [Data access](ACCESS.md) | Access routes, embargo and reuse conditions |
| [Zenodo deposit](ZENODO.md) | Deposit scope and relationship to institutional preservation |
| [Associated studies](PAPERS.md) | Study-specific clip sets and participant partitions |
| [Citation](CITATION.md) | Creator and dataset citation status |

The repository's two [illustrations](assets/) were generated as conceptual
figures. Their [generation prompts](assets/illustration_prompts.json) are retained
for provenance. Participant sensor recordings belong in the corresponding data
archive, outside this documentation repository.

## Verify supplied files

The utilities run with the Python standard library. Use the manifest and checksum
list supplied with the relevant approved archive version:

```sh
python tools/validate_manifest.py path/to/file_manifest.csv
python tools/verify_checksums.py path/to/download path/to/SHA256SUMS
```

Study reproduction additionally requires the exact versioned inputs, support masks
and participant allocations described in [PAPERS.md](PAPERS.md).

## Access, reuse and citation

Ogonna Okafor, Nottingham Trent University, is the confirmed dataset creator.
Access and reuse terms will accompany the dataset release. See [ACCESS.md](ACCESS.md)
and [CITATION.md](CITATION.md) for the current details.
