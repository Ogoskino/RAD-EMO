# RAD-EMO

**Multimodal laboratory recordings of object carriage and reported affect**

[**Request dataset access**](mailto:ogonna.okafor@ntu.ac.uk?subject=RAD-EMO%20dataset%20request)

RAD-EMO contains **114 recordings from 15 participants** across **38 protocol
conditions**, each recorded three times. Participants performed stationary and
walking activities while carrying a phone, a harmless replica knife, both objects
or neither. The protocol varies object placement and prompted affect, alongside
participants' self-reported valence, arousal and dominance.

The collection combines RGB and thermal images, millimetre-wave radar measurements,
pulse acquisition records, person annotations and recording labels. It supports
research on carried-object recognition, reported affect and multimodal sensing.

![RAD-EMO sensors and recording setup.](assets/rad_emo_dataset_overview.png)

*RAD-EMO collection overview.*

## Dataset contents

| Component | Coverage and format |
| --- | --- |
| RGB | 189,835 PNG images, 640 × 480 pixels, across 114 recordings |
| Thermal | 65,367 PNG images across 114 recordings |
| Radar ADC frames | 30,817 complex-sample MAT files across 76 recordings from 10 participants |
| Radar hardware | 77 GHz TI cascade MIMO system; 12 transmitters and 16 receivers |
| Original radar captures | Separate binary data, indexes and acquisition configuration for 90 recordings from 12 participants |
| Pulse and acquisition records | CSV tables alongside RGB frames; separate thermal acquisition tables |
| Annotations and labels | Available person annotations, protocol conditions and self-reported affect |

There are 60 stationary and 54 walking recordings. Modality availability varies
by recording; requested subsets can cover selected sensors or recording IDs.

## Dataset file structure

The existing collection is held in separate modality folders on institutional
OneDrive. The tree below shows their actual organisation, using participant 2,
recording 1 as an example with RGB, thermal and radar ADC frame files. Labels and
timing tables link the folders.

```text
RGB, pulse and radar ADC frames: rgb+mmw_exp2/
|-- P_2/
    |-- P_2_S_1/
        |-- color_frames/
        |   |-- color_frame_0.png
        |   |-- ...
        |-- data.csv
        |-- 2D-on-2D_annotations_export.json
        |-- Cascade_Capture_p2_s1/
            |-- adcData_frame_0001.mat
            |-- adcData_frame_0002.mat
            |-- ...

Thermal: thermal_data_exp2/
|-- P_2/
    |-- P_2_S_1/
        |-- thermal_frames/
        |   |-- thermal_frame_0.png
        |   |-- ...
        |-- data.csv

Labels and alignment: RAD_EMO_DATASET_v1.0.0/
|-- metadata/
|   |-- sessions.csv
|   |-- conditions.csv
|   |-- emotion_self_report.csv
|   |-- derived_labels.csv
|-- synchronization/
    |-- v2/
        |-- active_intervals.csv
        |-- dataset_view_v2.json
```

`P_2_S_1`, `Cascade_Capture_p2_s1` and the label-table ID `P2S1` refer to the
same recording. Other participants and recordings follow the corresponding
naming convention. Original RGB files for the first participant's first three
recordings also reside under `realsense_data/P_1/`; each frame is selected once
when assembling a requested subset.

Each `adcData_frame_*.mat` file contains the complex ADC samples for one radar
frame. These are processed ADC-level exports, before downstream FFT-based
representations. The original binary captures, their indexes and acquisition
configuration are stored separately under `PostProc - Copy/`; their coverage is
listed separately in the contents table.

## Labels and timing

Protocol labels record activity, object presence, placement and prompted affect.
Self-reported valence, arousal and dominance are recorded separately on 0–9 scales.
Low valence with high arousal is defined as `valence <= 4 and arousal >= 6`.

RGB and thermal acquisition tables retain frame timestamps. The saved RGB cadence
varies, with whole-recording rates of approximately 30–36 images per second.
Radar indexes retain relative frame timing; the alignment tables document
approximate association between sensors at recording and window level. Pulse
readings are logged alongside RGB acquisition rows.

![Recording conditions and reported-affect scales.](assets/rad_emo_recording_conditions.png)

*Object-carriage conditions and reported-affect scales.*

## Data access

The dataset is **available upon reasonable request for research purposes**.
Email [ogonna.okafor@ntu.ac.uk](mailto:ogonna.okafor@ntu.ac.uk?subject=RAD-EMO%20dataset%20request)
with your affiliation, intended research use and the sensors or recordings you
need. Requests are reviewed under the study's participant consent and data
management arrangements. Approved files are shared through a OneDrive link.

The button at the top opens an email request. This repository contains the
collection description and the two illustrations.

## Credits

**Creator and contact:** Ogonna Okafor, Nottingham Trent University.

**Contributors:** Maria Mwanje (data collection) and David Adama (supervision),
Nottingham Trent University.
