# Directory structure

## Documentation repository

These files are present in this repository. The illustrations are conceptual
figures; participant images, sensor arrays and private source crosswalks are
stored separately.

```text
RAD-EMO/
|-- README.md
|-- ACCESS.md
|-- CHANGELOG.md
|-- CITATION.md
|-- DATASET_CARD.md
|-- DATA_DICTIONARY.md
|-- PAPERS.md
|-- RAW_DATA.md
|-- ZENODO.md
|-- file_manifest.csv
|-- SHA256SUMS
|-- .gitattributes
|-- .gitignore
|-- assets/
|   |-- rad_emo_dataset_overview.png
|   |-- rad_emo_recording_conditions.png
|   |-- illustration_prompts.json
|-- docs/
|   |-- DIRECTORY_STRUCTURE.md
|-- metadata/
|   |-- collection_summary.json
|   |-- modality_inventory_snapshot.csv
|   |-- paper_subsets.csv
|   |-- raw_scope_summary.json
|   |-- release_status.json
|-- schemas/
|   |-- file_manifest.schema.json
|-- tools/
    |-- validate_manifest.py
    |-- verify_checksums.py
```

`metadata/` contains aggregate summaries and study support counts.
`file_manifest.csv` and its schema describe the supplied documentation files;
they are distinct from a future sensor-download manifest. The verification
utilities validate declared file metadata and checksums.

## Recording-level sensor archives

The following is the **proposed sensor archive path convention** used in local
release preparation. It describes how selected sensor files remain connected;
it is not a listing of currently downloadable data. Packages may be separated
into modality volumes while preserving the same recording identifiers.

```text
<sensor-volume>.zip
|-- manifest.json
|-- SHA256SUMS
|-- recordings/
    |-- <recording_id>/
        |-- rgb/
        |   |-- color_frames/
        |       |-- color_frame_<frame_id>.png
        |-- rgb_pulse_acquisition/
        |   |-- data.csv
        |-- thermal/
        |   |-- thermal_frames/
        |       |-- thermal_frame_<frame_id>.png
        |-- thermal_acquisition/
        |   |-- data.csv
        |-- radar_native/
        |   |-- <device>_<chunk>_data.bin
        |   |-- <device>_<chunk>_idx.bin
        |   |-- <capture_id>.mmwave.json
        |   |-- <capture_id>.setup.json
        |   |-- <capture_id>_LogFile.txt
        |-- annotations_source/
        |   |-- <available-annotation-export>.json
        |-- radar_adc_mat/                  # optional additional representation
            |-- <capture_id>/
                |-- adcData_frame_<frame_id>.mat
```

`<recording_id>` follows `P001_S001` formatting and preserves the recording join.
Native radar device names include `master`, `slave1`, `slave2` and `slave3`;
their chunk and index identities remain intact. The tree combines possible
contents across packages: each archive manifest identifies its actual files,
and unavailable or unselected modalities are not filled with placeholders.
The optional MAT representation has different coverage from native radar.

Recording catalogues, protocol/reported-label definitions, modality coverage and
timing-association metadata accompany the selected archive version. Camera and
thermal frame IDs join their acquisition tables; radar frames retain authentic
relative timestamps and the method/uncertainty of reconstructed cross-sensor
alignment. Original acquisition filenames and export paths are distinct fields.

Annotation exports describe their own generation and review status. A person box
is not a concealed-object box. Pulse fields accompany RGB acquisition rows; this
does not establish an independent, regularly sampled physiological reference.

The primary planned archive groups are native radar, RGB with acquisition and
annotation records, and thermal with acquisition records. Optional ADC MAT
volumes and research-ready derived inputs are separate layers. See
[RAW_DATA.md](../RAW_DATA.md), [DATA_DICTIONARY.md](../DATA_DICTIONARY.md) and
[ACCESS.md](../ACCESS.md) for their scope and access definitions.
