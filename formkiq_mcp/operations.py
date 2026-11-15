"""Reviewed operation effects, API groups, and deployment capabilities.

HTTP verbs and operation-name prefixes do not determine whether an operation
can mutate data. In particular, GetDocumentIdUpload grants write capability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Access = Literal["read", "write", "purge"]


@dataclass(frozen=True)
class OperationPolicy:
    access: Access
    api_group: str = "core"
    required_modules: frozenset[str] = frozenset()


def group(access: Access, api_group: str, names: str, *modules: str) -> dict[str, OperationPolicy]:
    return {name: OperationPolicy(access, api_group, frozenset(modules)) for name in names.split()}


OPERATION_POLICIES = {
    **group(
        "read",
        "core",
        """
        DocumentSearch GetAttribute GetAttributeAllowedValues GetAttributes
        GetDocument GetDocumentActions GetDocumentArtifacts GetDocumentAttribute
        GetDocumentAttributes GetDocumentContent GetDocuments GetDocumentTag
        GetDocumentTags GetDocumentUrl GetDocumentVersions GetVersion
        GetSites GetPermissionCatalog GetFolderDocuments IndexSearch
        GetSitesSchema GetSitesClassifications GetClassification
        GetSitesSchemaAttributeAllowedValues GetClassificationAttributeAllowedValues
    """,
    ),
    **group(
        "write",
        "core",
        """
        AddAttribute AddDocumentActions AddDocumentAttributes AddDocumentTags
        AddDocumentUpload DeleteAttribute DeleteDocument DeleteDocumentAttribute
        DeleteDocumentAttributeAndValue DeleteDocumentTag DeleteDocumentTagAndValue
        DeleteDocumentVersion SetDocumentAttributeValue SetDocumentAttributes
        SetDocumentRestore SetDocumentTag SetDocumentTags SetDocumentVersion
        UpdateAttribute UpdateDocument UpdateDocumentTags
        AddDocument GetDocumentIdUpload AddFolder AddDocumentRetryAction
    """,
    ),
    **group("purge", "core", "PurgeDocument"),
    **group("read", "audit", "GetResourceActivities"),
    **group("read", "ocr", "GetDocumentOcr"),
    **group("write", "ocr", "AddDocumentOcr"),
    **group("read", "fulltext", "SearchFulltext GetDocumentFulltext", "opensearch"),
    **group("read", "ai", "GetDocumentAiPromptsResults GetDocumentAiPromptResults", "ai"),
    **group("read", "malware", "GetMalwareScanResults", "malware"),
    **group(
        "read",
        "workflows",
        """
        GetWorkflows GetWorkflow GetDocumentWorkflows GetDocumentWorkflow
        GetQueues GetQueue GetWorkflowQueueDocuments GetWorkflowDocuments
    """,
        "workflows",
    ),
    **group("write", "workflows", "AddDocumentWorkflow AddDocumentWorkflowDecisions", "workflows"),
}

ALLOWED_OPERATION_IDS = frozenset(OPERATION_POLICIES)
READ_OPERATION_IDS = frozenset(name for name, policy in OPERATION_POLICIES.items() if policy.access == "read")
WRITE_OPERATION_IDS = frozenset(name for name, policy in OPERATION_POLICIES.items() if policy.access == "write")
API_GROUPS = frozenset(policy.api_group for policy in OPERATION_POLICIES.values())

# GetVersion returns MODULE_<name> settings with the MODULE_ prefix removed.
# A version-valued setting can appear as, for example, opensearch_2.19.2.
MODULE_ALIASES = {
    "opensearch": "opensearch",
    "typesense": "typesense",
    "textract": "textract",
    "ai": "ai",
    "dataclassification": "ai",
    "intelligent_document_processing": "ai",
    "malware": "malware",
    "malwarescan": "malware",
    "antivirus": "malware",
    "workflow": "workflows",
    "workflows": "workflows",
}


def module_capabilities(modules: list[str] | frozenset[str]) -> frozenset[str]:
    result = set()
    for value in modules:
        name = value.casefold().removeprefix("module_").replace("-", "_")
        for alias, capability in MODULE_ALIASES.items():
            if name == alias or re.fullmatch(re.escape(alias) + r"_\d[\w.]*", name):
                result.add(capability)
    return frozenset(result)
