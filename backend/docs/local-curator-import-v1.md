# Local Curator Import v1 — schema decision required

Implemented on product-lite-v1 as a separate admin tool at
`/admin/research/import/local-curator`, linked from Admin / Debug.
Historical JSONL import remains strict and unchanged.

The parser accepts UTF-8 metadata arrays and native Markdown ID/transcription
blocks. Identity is exact TikTok ID only. Small synthetic fixtures represent the
three supplied example IDs; the actual user's 20-record files were not present
in this workspace. SHA-256 covers canonical parsed data, excludes media_file,
and is independent of metadata ordering. Markdown record framing and boundary
newlines are excluded; transcript wording and internal whitespace are preserved.
No AI or network acquisition is involved.

Dry run is read-only. Confirmation uses expiring, single-use, process-local
pending data, consistent with the existing historical importer. A restart or
routing to another API process requires uploading again. Confirmation rechecks
identity and transcript presence and writes assets transactionally. PostgreSQL
advisory locking serializes native confirmations; existing unique stable-channel,
video and transcript constraints guard concurrent scan/import creation. A unique
constraint race returns a conflict and rolls back the whole transaction.

## Blocking metric schema limitation

`VideoSnapshot.analysis_id`, all five counts, all rates, outlier_score,
overall_rank and transcript_eligible are non-nullable. Snapshots define analysis
scan membership (`analysis_ranked_videos`) and enter channel-wide ranking
(`latest_channel_rows`, `global_ranked_videos`). Analysis requires scan median,
video count and requested transcript count. Neither model has a partial local
observation source/type. There is no safe existing destination for views/likes.

No migration, model alteration or fake Analysis/VideoSnapshot was created.
Metric-bearing bundles are previewable but confirmation is blocked; metrics
are neither dropped nor converted into fabricated complete scan observations.
METRIC_NEW_OBSERVATION and METRIC_UNCHANGED remain zero, with the count of
pending observations shown separately. Identical observation deduplication is
pending the schema decision, rather than claimed as implemented.

Proposed minimal change, for review before implementation: add an independent
`video_metric_observations` table with UUID identity, video foreign key, source
(`local-curator-import-v1`), observed_at (import time), nullable views and likes,
bundle_sha256 for diagnostics, and an observation fingerprint. Enforce uniqueness
on (video_id, source, fingerprint), hashing the known metric keys and values,
including nulls. Check counts are nonnegative and at least one is known. Do not
add comments/shares/favorites, engagement, rank or analysis membership to this
table. Identical evidence is reused on subsequent bundles; changed views/likes
create a new observation at import time. Existing scan queries continue reading
VideoSnapshot exclusively. This preserves channel medians, priority pools and
scan completeness. No new bundle registry is needed.

## Unknown publication dates

`Video.published_at` is non-nullable. Parsing accepts null dates; creating a new
video with an unknown date is explicitly blocked. A second minimal proposed
change is making this column nullable and auditing consumers for null dates.
No fabricated publication date or import timestamp is substituted. Existing
videos with known dates can still be reused when metadata omits the date.

## Implemented asset-only path and future merge

For bundles with no known metrics and known dates for new videos, confirmation
can create/reuse Channel and Video assets and missing Transcript rows. Stable
account identity wins, incompatible identities and cross-channel video IDs are
hard conflicts. Canonical usernames and populated video metadata are preserved.
Transcript provenance is `local-curator-import-v1`; existing text is never
replaced. Metadata-only rows do not resolve transcription. Orphan transcript IDs
are ignored with warnings. Re-import creates no duplicate assets.

No snapshots, analysis rows, transcription jobs or intelligence are generated.
Normal scans already resolve global videos by TikTok ID, create proper scan
snapshots, and use Transcript presence to prevent acquisition. Regression tests
exercise this merge with the native representative video IDs.

## Deployment gate

A migration is required for full V1 metric persistence. Do not build/deploy this
preview or apply migrations yet. Current API/worker services are left untouched.
A real 20-video dry run awaits access to the user's actual native files and a
subsequent preview deployment; synthetic fixture counts are not real corpus
counts.
