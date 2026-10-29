# Papers and exact analysis subsets

One collection supports three related studies. Each archive release should bind
the paper version, input hashes and exact retained clip rows. The table below
describes the current manuscript versions; the manuscripts remain unpublished
drafts in the material reviewed for this release.

| Study | Manuscript | Retained scope |
| --- | --- | --- |
| Joint recognition | *Joint Recognition of Knife Carriage and Reported Affect from Radar and Video*, revision 17 | 178 joint development clips; 94 selection observations within that pool; 167 transfer clips |
| Reported affect | *Learning Reported Affect from Video through Comparisons between Recordings*, revision 6 | 271 development clips from nine participants; 167 transfer clips from six other participants |
| Radar carriage | *Person and Surroundings Radar Representations for Knife Carriage Recognition*, revision 6 | Original radar fitting/selection protocol; 178 original transfer clips |

## Joint recognition

Affect development uses nine participants. Six of them have the associated radar
inputs used for joint development. Joint selection uses three participants and
is contained within the 178-clip joint development pool; its 94 observations are
not an additional test set. Two selection participants previously contributed to
radar selection. The full historical selection chain is therefore not independent.

The six transfer participants are distinct from the development participants.
The 167 common transfer clips contain 32 joint-positive clips from eight
recordings and three participants, and 135 joint-negative clips. The final joint
heads use the retained original radar component, not a later full-development
radar refit.

## Affect recognition

The common set contains 438 clips from 113 recordings, partitioned into 271
development and 167 transfer clips. The affect paper's primary recording-comparison
learner is distinct from the historical FB/PFB descriptor branch retained by the
joint system. A shared collection does not make these the same model or the same
training recipe.

## Radar recognition

The original radar fitting set comprises 21 recordings from three participants,
with five blocks each (105 clips). Selection and transfer use four central radar
blocks per recording. The original selection set contains 68 clips from 17
recordings and two other participants. The original transfer set contains 178
supported clips. The five-block fitting convention and radar support rules differ
from the four central video-window convention used for joint analysis. The same
21 fitting recordings contribute 84 central windows to the joint input bank.

## What to preserve in a release

Every paper-specific clip row must retain its recording join key, participant
group, source window convention, input support and historical fitting/selection
role. Fold membership must describe the relevant model stage. Development rows
must not be relabelled as untouched transfer data, and the six-person transfer
group must not be described as joint-head training data.

Participant-level manifests are prepared in the restricted research-ready candidate;
they are not included in this documentation-only repository. Aggregate support
is available in [paper_subsets.csv](metadata/paper_subsets.csv).
