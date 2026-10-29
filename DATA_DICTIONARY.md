# Data dictionary

These are release schemas. Supplied files must declare their actual version and
access tier; a schema is not evidence that the associated payload has been
released. Participant identifiers preserve grouping and joins, not anonymity.

## Recording manifest

| Field | Meaning |
| --- | --- |
| session_id | Stable recording identifier; distinct from a clip identifier |
| participant_id | Consistent pseudonymous participant group |
| condition_id | Recording-protocol condition |
| activity_protocol | Still or walking as specified in the protocol |
| knife_present_protocol | Binary protocol label K |
| phone_present_protocol | Binary phone condition |
| carry_visibility_protocol | Intended placement/visibility; not a framewise observation |
| adherence_status | Review status of protocol adherence |
| lvha_label | Binary L derived from valence ≤ 4 and arousal ≥ 6 |
| joint_state | 1 + L + 2K; categorical T1–T4 |
| access_tier | Approved tier for this table; no default from pseudonymisation |

Individual questionnaire values and private source paths are separate restricted
records. They are not required in public aggregate metadata.

## Paper clip manifest

| Field | Meaning |
| --- | --- |
| paper_id / paper_revision | Manuscript and version to which the row belongs |
| clip_id / session_id | Clip identity and recording join |
| participant_id | Group for participant-separated fitting/evaluation |
| window_convention / window_id | Source-specific window scheme and index |
| partition | Development or transfer for this study |
| model_stage / fold_id | Stage-specific fitting, selection or evaluation role |
| input_support | Available inputs and reason for retention/exclusion |
| video_start_s / video_end_s | Relative video interval, where defined |
| radar_block_start / radar_block_end | Actual radar frame-index interval, where available |
| timing_method / timing_uncertainty_s | Reconstruction method and assumed uncertainty |

Frame indices and observation masks must be supplied in linked tables or arrays
where they exist. Unknown intervals must remain unknown; nominal five-second
video windows must not be copied into irregular radar durations.

## File manifest

The [file schema](schemas/file_manifest.schema.json) describes an approved
download manifest. It uses a POSIX relative path, size in bytes, SHA256 hash,
modality, data level and explicit access tier. Paths must stay within the download
directory. Local absolute paths, calendar acquisition times, names and linkage
keys are excluded from disseminated manifests.

## Array metadata

Each array export must identify the following alongside the file manifest:

- Array key, shape, dtype and named axes in stored order.
- Units or the explicit unitless transform and normalisation.
- Row-to-recording/clip mapping, coordinate convention and observation masks.
- Extraction implementation/version and source checksum dependencies.
- Fitting population for learned transformations and frozen model components.
- Clock domain, relative origin, reconstruction method and uncertainty.

RAE denotes range, azimuth and elevation; ARD denotes azimuth, range and Doppler.
The declared stored axis order takes precedence over an acronym. Radar arrays
after board processing are not unprocessed ADC binaries. Video embeddings,
body descriptors and RGB-derived waveforms need separate disclosure assessment.
