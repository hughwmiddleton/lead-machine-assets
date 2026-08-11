"""Additive Lead Engine V1 contract foundation.

This package is intentionally not imported by the live Lead Machine runtime.
"""

from .adapters import lead_record_from_row, source_occurrence_from_row
from .contracts import Evidence, LeadRecord, SourceOccurrence
from .identity import InsufficientIdentityEvidence
from .identity_evidence import (
    IdentityProfile,
    IdentitySignal,
    identity_profile_from_contracts,
    identity_profile_from_row,
)
from .export_ledger import (
    CampaignExport,
    CampaignExportLedger,
    CampaignExportRow,
    ExportField,
    ExportLineage,
    ExportedContactDestination,
    build_campaign_export,
    export_id,
    export_row_id,
)
from .registry import (
    HumanDecisionType,
    HumanIdentityDecision,
    IdentityAssertion,
    IdentityClassification,
    IdentityEvidenceRegistry,
    assertion_id,
    compare_identity_profiles,
    compare_identity_rows,
)

__all__ = [
    "Evidence",
    "CampaignExport",
    "CampaignExportLedger",
    "CampaignExportRow",
    "ExportField",
    "ExportLineage",
    "ExportedContactDestination",
    "InsufficientIdentityEvidence",
    "HumanDecisionType",
    "HumanIdentityDecision",
    "IdentityAssertion",
    "IdentityClassification",
    "IdentityEvidenceRegistry",
    "IdentityProfile",
    "IdentitySignal",
    "LeadRecord",
    "SourceOccurrence",
    "lead_record_from_row",
    "assertion_id",
    "compare_identity_profiles",
    "compare_identity_rows",
    "build_campaign_export",
    "export_id",
    "export_row_id",
    "identity_profile_from_row",
    "identity_profile_from_contracts",
    "source_occurrence_from_row",
]
