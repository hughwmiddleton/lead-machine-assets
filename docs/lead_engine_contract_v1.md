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
rows leave Studiflow as outbound CSV content. Its pure contracts do not contact
Woodpecker; the narrow Campaign Prep persistence integration is documented
below.

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
email, row fingerprint, or batch content fingerprint. The pure contract still
has no automatic file writer, production database, deduplication, or analytics
event model.

### Campaign Prep runtime integration

For the Woodpecker profile, one invocation of Campaign Prep is one intentional
export operation. A fresh `campaign-prep:<uuid4>` operation reference is
created once per user action and reused across every recency and combined CSV
artifact. Supplying that reference again is the explicit retry/idempotency
mechanism; an exact retry also supplies the original operation timestamp. A
second deliberate action gets another UUID even when its content is identical.
This event UUID is not a deterministic lead or source identity.

One UTC, timezone-aware operation timestamp is captured at the start of the
action and supplied to the frozen ledger. It is shared by all export rows and
does not participate in export identity. The independent Campaign Prep
recency-reference time retains its existing semantics.

After all legacy output files, summary, and `campaign_export_manifest.csv`
finish successfully, Campaign Prep writes the additive, versioned
`campaign_export_ledger.json`. The existing manifest is unchanged. The JSON
contains the frozen ledger serialization plus an integration-layer `artifacts`
list. Each artifact entry records its relative filename, byte size, SHA-256 of
the final bytes on disk, row count, global ledger row-position interval,
`export_id`, and exact ordered `export_row_ids`. This wrapper represents the
one-operation/many-file shape without changing the core export identifier.

Ledger values are constructed from CSV rows re-read after their atomic writes,
so exported fields and row order describe the actual files. Source rows are
used only to retain lineage evidence that the fixed Woodpecker projection does
not export. Split emails and duplicates remain separate occurrences. Explicit
validated IDs and strong source-native evidence can resolve; weak, invalid, or
conflicting evidence remains unresolved under the frozen adapter rules.

The sidecar is serialized as canonical UTF-8 JSON and persisted through a
same-directory temporary file, flush/fsync, and atomic replacement. A prior
sidecar is invalidated before CSV overwrite begins. CSV failure or a partial
multi-artifact write therefore produces no completed sidecar. An empty action
with no campaign artifact produces no sidecar and claims no exported rows.
If sidecar construction or persistence fails after the CSV workflow succeeds,
the CSVs remain intact, temporary/final sidecar files are removed, and Campaign
Prep reports visible degraded success rather than claiming tracked completion.

Campaign CSV columns, values, bytes, filenames, filtering, segmentation,
release-date ordering, split-email behavior, duplicate handling, and the
legacy manifest remain behavior-compatible. Woodpecker upload remains manual
and external. No Woodpecker campaign/prospect IDs, outreach or analytics
events, CRM outcomes, canonical entities, or portal features are created here.

A future outreach linkage stage can attach Gmail conversation evidence to
`export_row_id`, supported by exported email, profile, artifact checksum and
filename, and lineage metadata. That can later power the `LEADS EXPORTED` step
in customer funnel reporting without changing these identifier semantics.
Woodpecker API integration, live Gmail access, production event history,
canonical clustering, portal reporting, and customer-specific ranking remain
out of scope.

## Gmail outreach attribution and future CRM handoff

### Audited production boundary

The confirmed operating flow is:

```text
Lead Engine -> Campaign Prep -> Woodpecker CSV -> manual Woodpecker upload
            -> Woodpecker sends through the user's Gmail account
            -> Gmail sent message/thread -> inbound reply -> CRM process
```

Woodpecker is the outbound transport/execution layer in this flow. Gmail is the
conversation evidence surface. Neither is the authoritative Lead Engine
person, artist, or relationship identity. The CRM owns the relationship after
engagement.

Repository evidence establishes the Campaign Prep projection and the export
sidecar, including the actual CSV filename/checksum and ordered
`export_row_id`s. This repository contains no Gmail API client, sent-message or
thread ingestion, RFC Message-ID handling, reply detector, Gmail contact
matching, CRM contact creation, CRM lifecycle policy, or CRM mutation code.
Those implementation and policy details are external and must be audited in
their owning repository or integration before use.

The fixed Woodpecker CSV contains destination and personalization columns, but
does not contain `export_row_id`, `lead_id`, `outreach_attempt_id`, a Gmail
message/thread ID, or another demonstrated durable correlation marker. The
sidecar is local evidence and is not shown to travel through Woodpecker into
Gmail. Consequently an export row cannot currently be deterministically linked
to its resulting Gmail message from repository evidence alone.

There is no repository proof that a Woodpecker custom field survives into an
invisible Gmail header or other durable Gmail metadata. Existing subject/body
template configuration is also absent. A future marker may be technically
possible, but feasibility, privacy, visibility, template behavior, reply
survival, and Gmail searchability must be verified against the real outbound
integration before that approach is selected.

### Outreach attempt identity

`OutreachAttempt` represents one intentional outbound attempt associated with
one `CampaignExportRow`. Its deterministic ID hashes only `export_row_id` and
has the form `le:outreach-attempt:v1:<sha256>`. The invariant is one export row
to one intended attempt. It does not mean that Woodpecker accepted or sent the
row. Re-exporting the same email under a different export operation creates a
different export row and therefore a different attempt. Split-email rows also
remain distinct attempts.

The attempt copies, without re-resolving, the frozen export ID, export row ID,
row fingerprint, exported destination, export timestamp, lineage status,
`lead_id`, and `source_occurrence_id`. Unresolved lineage remains unresolved.
An attempt is not a Gmail contact, person, canonical artist, CRM relationship,
or proof of delivery.

### Gmail provider references

`GmailMessageRef` preserves the provider-owned Gmail message ID and thread ID,
optional RFC Message-ID, direction, normalized envelope sender/recipients,
exact timezone-aware event timestamp, and optional subject/body fingerprints.
`GmailThreadRef` preserves the provider thread ID and narrow participant
references. Gmail IDs never replace Lead Engine, export-row, or outreach
attempt IDs. A thread may contain several messages and may predate the campaign.

Subject/body content can be reduced to deterministic SHA-256 fingerprints after
whitespace normalization. Full message bodies are not part of this contract.
Fingerprint compatibility is corroboration only unless a future integration
proves that a field is an explicit persisted mapping.

### Explainable attribution hierarchy

`OutreachAttribution` relates one outbound Gmail message/thread to zero or one
selected outreach attempt while retaining all candidate attempt IDs and named
evidence. It uses explicit rule categories rather than an opaque score:

- `EXACT`: one durable explicit marker or persisted mapping proves the row to
  message link and no strong conflict defeats that candidate.
- `HIGH_CONFIDENCE`: exactly one plausible attempt has an exact recipient plus
  at least one other independent strong fact, such as the audited sending
  account or a compatible campaign content fingerprint.
- `HEURISTIC`: one candidate has only weak or insufficiently independent
  support. Email alone and export-time proximity alone belong here.
- `AMBIGUOUS`: multiple attempts plausibly match, including repeated campaigns
  to the same address or multiple explicit mappings.
- `CONFLICT`: strong evidence contradicts the proposed mapping, such as an
  incompatible envelope recipient.
- `UNMATCHED`: no supported candidate exists.

Evidence retains its kind, relationship, strength, reason code, independence
key, and narrow value. Time comparisons additionally retain the signed delta
and caller-supplied tolerance. Export time is not send time: manual upload,
scheduling, and execution delays mean export timestamp proximity is always
weak in this V1 contract. All timestamps must be timezone-aware and serialize
in UTC.

Email is evidence, not identity. It may generate a candidate but cannot prove
Lead Engine identity or exact attribution. Shared booking/management addresses,
multiple artists per inbox, prior contacts, and repeated campaigns prevent that
shortcut. The evaluator also does not search Gmail history automatically: a
pre-existing thread with the same participant remains unmatched unless
campaign-origin evidence is supplied. It never chooses the newest or oldest
export merely because dates or recipients coincide.

### Reply observation

`ReplyObservation` means only that Gmail observed an inbound message on the
same thread as a resolved outreach attribution. Its deterministic ID derives
from the inbound Gmail message and attribution IDs. It records provider
references, sender/recipients, observed time, and the inherited attribution
classification. It does not infer positive/negative sentiment, interest,
qualification, opportunity, booking, or lifecycle state.

### CRM handoff boundary

`EngagedLeadHandoff` is a provider-neutral future boundary object. It carries
the available source occurrence, lead, export, export row, outreach attempt,
Gmail thread, outbound message, reply message, contacted email, attribution,
and reply-observation references. An unresolved Lead Engine lineage remains
explicitly unresolved with blank IDs; Gmail IDs are never promoted into those
fields.

This contract neither creates a CRM contact nor authorizes automatic creation
on reply. The CRM remains responsible for contact/relationship records,
lifecycle and stage, queues, follow-up scheduling, drafting, and conversation
management. A later audit must determine how the existing human/CRM process
accepts this handoff. Lead Engine remains responsible for discovery provenance,
lead identity, export lineage, and outreach-origin attribution; Gmail is the
evidence bridge.

### Registry, analytics, and non-goals

`OutreachAttributionRegistry` is an in-memory deterministic index of attempts,
Gmail references, attributions, and replies. It retrieves by export row, Gmail
message, and Gmail thread, and exposes competing mappings rather than silently
overwriting them. Serialization is stable. It is not production persistence
and performs no network, Gmail, Woodpecker, or CRM action.

This boundary can eventually support explainable counts from exported lead to
attempt, sent conversation, reply, and accepted CRM handoff. It does not yet
provide customer analytics, delivery metrics, live Woodpecker analytics,
sentiment/intent, opportunity or client conversion, a customer portal, or
canonical artist clustering. Those later measures must preserve attribution
classification and unresolved/conflicting provenance rather than flattening
all email matches into customers.
