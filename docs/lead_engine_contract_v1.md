# Lead Engine Contract V1

This package is an additive, side-effect-free identity envelope for existing
Lead Machine row data. Existing pipelines remain authoritative at runtime and
do not import or invoke it.

## Identifier semantics

`source_occurrence_id` identifies one source observation. Its format is
`le:source-occurrence:v1:<source-type>:<sha256>`. The digest is over canonical
JSON containing `identity_version`, normalized `source_type`, `identity kind`,
and normalized identity `value`. Evidence selection prefers, in order:

- Spotify Artist ID (explicit or from an `/artist/<id>` URL)
- an explicitly labelled `Source Native ID` for another source
- Bandcamp artist host (or a conservative profile URL for non-Bandcamp hosts)
- SoundCloud profile handle
- Triple J Unearthed artist slug
- Last.fm `/music/<artist>` path identity
- a canonical source URL
- an explicitly `weak` source/name/location fallback

Festival rows use the lineup URL plus normalized artist name and are explicitly
`weak`; this represents a lineup observation, not an artist identity. Mutable
enrichment, email, review state, timestamps, run IDs, and job IDs do not
participate in the identifier.

Text uses Unicode NFKC normalization and collapsed whitespace; comparison-only
fallback fields are casefolded. URL normalization accepts HTTP(S), lowercases
the scheme/host, removes default ports, duplicate/trailing path slashes and
fragments, and conservatively preserves query strings. Platform-native keys
ignore non-identity URL presentation components. The weak generic fallback
hashes normalized artist name, location, and source-directory context; blanks
remain blank in that payload.

`lead_id` identifies the V1 Lead Engine record derived from exactly one source
occurrence. Its format is `le:lead:v1:<sha256>`, with the digest taken over the
`source_occurrence_id`. It is not a cross-source identity.

There is intentionally no `canonical_entity_id`. Canonical entity resolution is
outside this phase, and normalized artist names never create canonical IDs.

## Provenance

`Evidence` records a fact name, its original scalar value, the source occurrence
reference, source type/URL, evidence type, observation time, and optional
explicit confidence/extraction metadata. Blank metadata stays blank. The row
adapter emits only representative evidence for present artist, location,
country, email, and Spotify native-ID fields; it does not convert every column
or infer missing facts.

## Deliberate non-goals

V1 does not solve cross-source or confirmed cross-run entity resolution, CRM or
Woodpecker prospect identity, campaign/export history, ranking, customer
analytics, or customer-specific quality scoring.

## Migration principle

Lead Machine discovery, enrichment, Night Mode, Lead Vault, Campaign Prep, and
Woodpecker export remain unchanged and authoritative. Later phases may adopt
these contracts incrementally at explicit boundaries.

## Identity evidence registry

The additive registry evaluates possible relationships between two source
occurrences. An `IdentityAssertion` is an inspectable, pairwise advisory result;
it is not a merged lead or entity. Its ID has the form
`le:identity-assertion:v1:<sha256>` and hashes canonical JSON containing only the
lexically sorted pair of source-occurrence IDs. Reversing the pair, changing
evidence, re-evaluating rules, or adding a human decision cannot change the
assertion ID. Repeated inputs that already resolve to the same occurrence ID may
produce a reflexive exact assertion.

`IdentityProfile` extracts de-duplicated signals from existing row fields while
retaining raw values and the originating source-occurrence ID. Signal families
are:

- `provider`: Spotify IDs and canonical Bandcamp, SoundCloud, Unearthed,
  Last.fm, or explicitly namespaced provider-native identities
- `social`: canonical Instagram handles and Facebook profiles
- `website`: website domains, strong only when `Domain_Role` explicitly marks
  the domain artist-controlled; link hubs are excluded
- `contact`: email, strong only when both `Contact_Role` is artist/self and
  `Contact_Type` is direct; otherwise shared or unclassified and weak
- `context`: normalized artist name and location

Profiles can also be derived directly from an established `SourceOccurrence`
and `LeadRecord`, including recognized facts in its `Evidence` tuple. That path
validates the record reference and fails if reconstruction would change the
existing `source_occurrence_id`.

Copies of the same normalized signal collapse to one independence key. Thus an
Instagram URL, handle, and repeated External Links entry are one fact, not
three. Website and social facts remain independent families. No numerical
identity score is used.

### Pairwise classifications and reason codes

- `EXACT`: the pair shares genuinely provider-native identity and has no strong
  provider/artist-domain conflict.
- `HIGH_CONFIDENCE`: the normalized artist name matches and at least two strong,
  independent corroborating families among social, artist-controlled website,
  and explicitly direct contact match.
- `HEURISTIC`: contextual evidence such as the same name or name/location
  suggests review but cannot authorize identity by itself.
- `CONFLICT`: comparable provider-native identities differ, or matching names
  carry explicitly incompatible artist-controlled domains.
- `INSUFFICIENT`: there is not enough evidence either way.

Assertions expose named reasons such as `same_spotify_artist_id`,
`same_instagram_handle`, `same_artist_domain`, `same_direct_email`,
`same_shared_email`, `same_artist_name`, `same_location`,
`different_spotify_artist_id`, `different_artist_domain`, and
`location_conflict`. The evidence entries behind each reason retain normalized
left/right values, strength, family, and independence key. Contextual similarity
never hides a strong provider conflict.

### Human decisions and auto-join advice

`HumanIdentityDecision` supports `CONFIRMED_SAME`, `CONFIRMED_DIFFERENT`, and
`UNRESOLVED`, with optional reason, actor, and supplied timestamp. Automated
classification remains visible after review. The decision changes only the
effective outcome and advisory `auto_join_eligible` flag:

- `CONFIRMED_SAME` overrides uncertainty with an effective same outcome.
- `CONFIRMED_DIFFERENT` prevents auto-join regardless of automated confidence.
- `UNRESOLVED` clears the override back to automated advice.

Effective outcomes deliberately preserve that distinction: automated results
serialize as `ADVISORY_SAME`, `ADVISORY_DIFFERENT`, or `UNRESOLVED`; only human
decisions produce `CONFIRMED_SAME` or `CONFIRMED_DIFFERENT`.

Without a human decision, `EXACT` is advisory auto-join eligible and
`HIGH_CONFIDENCE` is eligible only because its explicit two-family rule has
passed. `HEURISTIC`, `CONFLICT`, and `INSUFFICIENT` are never eligible. Nothing
in this phase executes a join.

### Pairwise and transitivity boundary

`IdentityEvidenceRegistry` stores assertions in memory and serializes them in
assertion-ID order. It deliberately exposes no clustering or canonical-entity
API. If A strongly matches B and B strongly matches C while A conflicts with C,
all three pairwise assertions remain present. The registry must not infer an
A+B+C entity. Human overrides are likewise pair-specific and reversible.

Canonical entity creation, graph clustering, production cross-run persistence,
CRM/Woodpecker identity, campaign history, analytics, customer portal reporting,
and customer-specific lead ranking remain future work.

## Campaign Export Ledger

The additive Campaign Export Ledger owns only the point where approved lead
rows leave Studiflow as outbound CSV content. It is not wired into Campaign
Prep and does not contact Woodpecker.

### Existing Campaign Prep boundary

Campaign Prep currently offers `lead_machine_full`, `woodpecker`, and
`input_headers` formats. Its Woodpecker projection contains Email, First Name,
Company, Artist, Location, Song Title, Sounds Like, Website, Instagram,
Facebook, Source URL, both origin-directory variants, release/upload dates,
Notes, and the added `Recency_Bucket`. It may filter missing email, split one
comma-separated email cell into multiple rows, and deliberately preserves
duplicates. Input order is retained unless the stable release-date sort is
requested.

Region, radio-play, and recency determine filenames. The existing
`campaign_export_manifest.csv` inventories filenames and row counts only; it
does not provide operation, row, lineage, content, campaign, or reconciliation
identity. The runtime reference timestamp is diagnostic rather than an export
identity, and no campaign label, Woodpecker campaign/prospect ID, or historical
result-return manifest exists.

### Export operation and content identity

`CampaignExport` represents one intentional export operation. Its `export_id`
has the form `le:campaign-export:v1:<sha256>` and hashes canonical JSON
containing only a required caller-supplied `operation_reference` after Unicode
and whitespace normalization. The reference is an idempotency key: retries of
one operation reuse it, while exporting identical content intentionally a
second time requires a different reference. No clock-derived or random ID is
created. `created_at` is required explicitly, must be timezone-aware, and is
normalized to UTC without participating in operation identity.

`content_fingerprint` has the form `le:export-content:v1:<sha256>`. It hashes
the destination type, export profile and profile-schema version, plus the
sorted multiset of outbound row fingerprints. Sorting makes CSV ordering
irrelevant to content comparison while retaining duplicate multiplicity. Thus
two operations can have different export IDs and identical content
fingerprints.

### Export row identity and fingerprint

`CampaignExportRow` represents one exact one-based row position in one export.
Its ID has the form `le:campaign-export-row:v1:<sha256>` and hashes only
`export_id` plus `row_position`. It is a reconciliation key, not a person or
artist ID. Identical content exported in another operation receives a different
row ID.

The row fingerprint has the form `le:outbound-row:v1:<sha256>` and covers:

- destination type
- export profile and profile-schema version
- the fixed Campaign Prep Woodpecker projection listed above
- normalized exported email and URLs
- whitespace-normalized personalization text
- casefolded origin/profile tokens

Header aliases, header order, CSV quoting, surrounding email whitespace,
duplicate display whitespace, URL host/scheme presentation, export operation,
row position, timestamp, campaign label, and Lead Engine lineage do not
participate. Artist/personalization casing remains material. Lead lineage is
excluded deliberately so materially identical outbound content can be detected
even when one legacy row lacks lineage. Different artists sharing one email do
not collapse because the remaining outbound fields participate. Fingerprint
matches are discoverable but never silently deduplicated.

### Lead lineage and legacy safety

`ExportLineage` preserves validated `lead_id` and `source_occurrence_id`
references independently of outbound content. A supplied V1 pair must satisfy
the established one-to-one lead derivation. A valid occurrence-only reference
may safely derive its V1 lead ID; a lead-only reference remains `PARTIAL`.
Invalid or conflicting references remain `UNRESOLVED` with their raw values.
Identity assertion IDs are never accepted as lead IDs.

For legacy rows with no explicit identifiers, lineage is derived only when the
existing adapter finds strong source-specific native evidence: Spotify,
Bandcamp, SoundCloud, Unearthed, Last.fm, or an explicitly named provider ID.
Generic source URLs, festival/name composites, and name/location/directory
fallbacks remain `UNRESOLVED`. No canonical identity is fabricated.

`ExportedContactDestination` separately preserves the raw email, normalized
email, explicitly supplied contact type, and available email source URL/type
and extraction method. It does not infer artist ownership from an address and
does not use shared contact as identity evidence.

### Ledger and future event attachment

`CampaignExportLedger` is an in-memory deterministic index for exports and
rows. It can retrieve by export/row ID and find rows by lead ID, normalized
email, row fingerprint, or batch content fingerprint. It has no automatic file
writer, production database, deduplication, or analytics event model.

A future Woodpecker reconciliation stage can attach provider campaign/prospect
IDs and delivery/reply events to `export_row_id`, supported by exported email,
profile, campaign label, filename, and lineage metadata. That can later power
the `LEADS EXPORTED` step in customer funnel reporting without changing these
identifier semantics. Woodpecker API integration, event history, CRM handoff,
canonical clustering, portal reporting, and customer-specific ranking remain
out of scope.
