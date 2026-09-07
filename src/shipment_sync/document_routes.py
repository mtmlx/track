from __future__ import annotations

from datetime import date
import re
import secrets
from typing import Annotated, Any
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel
import requests

from shipment_sync.docuseal_client import DocuSealClient
from shipment_sync.document_config import (
    DocumentIntegrationConfigReport,
    DocumentIntegrationSettings,
    inspect_document_integration_env,
)


class DocumentRequirementsResponse(BaseModel):
    configured: bool
    live_ready: bool
    required_items: list[str]
    missing_required_items: list[str]
    recommended_items: list[str]
    missing_recommended_items: list[str]
    notes: list[str]


class DocumentParty(BaseModel):
    company_name: str
    company_address: str | None = None
    company_tax_id: str | None = None


class DocumentSigner(BaseModel):
    name: str
    email: str
    title: str | None = None
    phone: str | None = None


class NdaIssueRequest(BaseModel):
    counterparty: DocumentParty
    counterparty_signer: DocumentSigner
    mtm_signer: DocumentSigner | None = None
    effective_date: date | None = None
    external_reference: str | None = None
    send_email: bool | None = None
    dry_run: bool = True
    metadata: dict[str, Any] | None = None


class CreditContractTriggerRequest(BaseModel):
    task_id: str | None = None
    task_url: str | None = None
    customer_company_name: str | None = None
    signer_name: str | None = None
    signer_email: str | None = None
    external_reference: str | None = None
    send_email: bool | None = None
    dry_run: bool = True
    payload: dict[str, Any] | None = None


class DocumentSubmissionResponse(BaseModel):
    document_type: str
    dry_run: bool
    external_id: str
    docuseal_submission_id: int | None = None
    docuseal_submitter_ids: list[int] = []
    signing_urls: list[str] = []
    missing_required_fields: list[str] = []
    docuseal_payload: dict[str, Any] | None = None
    docuseal_response: Any | None = None


class DocuSealWebhookResponse(BaseModel):
    accepted: bool
    event_type: str | None
    document_type: str | None
    submission_id: int | None
    submitter_id: int | None
    external_id: str | None
    clickup_task_id: str | None
    documents_count: int
    action: str


def register_document_routes(app: FastAPI) -> None:
    @app.get("/api/documents/requirements", response_model=DocumentRequirementsResponse)
    def document_requirements() -> DocumentRequirementsResponse:
        load_dotenv()
        return _serialize_requirements(inspect_document_integration_env())

    @app.post("/api/documents/nda", response_model=DocumentSubmissionResponse)
    def issue_nda(
        body: NdaIssueRequest,
        settings: Annotated[DocumentIntegrationSettings, Depends(_get_document_settings)],
        _: None = Depends(_require_operator_auth),
    ) -> DocumentSubmissionResponse:
        missing = _missing_nda_fields(body, settings)
        if missing:
            raise HTTPException(status_code=400, detail="Missing required NDA fields: " + ", ".join(missing))
        if not settings.docuseal_nda_template_id:
            raise HTTPException(status_code=503, detail="DOCUSEAL_NDA_TEMPLATE_ID is not configured")

        external_id = _external_id("nda", body.external_reference, body.counterparty.company_name)
        payload = _build_nda_submission_payload(body, settings, external_id)
        if body.dry_run:
            return DocumentSubmissionResponse(
                document_type="nda",
                dry_run=True,
                external_id=external_id,
                docuseal_payload=payload,
            )

        client = _get_docuseal_client(settings)
        try:
            response = client.create_submission(payload)
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"DocuSeal request failed: {exc}") from exc

        return _submission_response("nda", external_id, dry_run=False, docuseal_response=response)

    @app.post("/api/webhooks/clickup/credit-contract", response_model=DocumentSubmissionResponse)
    def clickup_credit_contract_webhook(
        body: CreditContractTriggerRequest,
        settings: Annotated[DocumentIntegrationSettings, Depends(_get_document_settings)],
        _: None = Depends(_require_operator_auth),
    ) -> DocumentSubmissionResponse:
        normalized = _normalize_credit_contract_request(body)
        missing = _missing_credit_contract_fields(normalized, settings)
        external_id = _external_id(
            "credit-contract",
            normalized.external_reference or normalized.task_id,
            normalized.customer_company_name,
        )
        if missing:
            return DocumentSubmissionResponse(
                document_type="credit_contract",
                dry_run=body.dry_run,
                external_id=external_id,
                missing_required_fields=missing,
            )
        if not settings.docuseal_credit_contract_template_id:
            raise HTTPException(status_code=503, detail="DOCUSEAL_CREDIT_CONTRACT_TEMPLATE_ID is not configured")

        payload = _build_credit_contract_submission_payload(normalized, settings, external_id)
        if normalized.dry_run:
            return DocumentSubmissionResponse(
                document_type="credit_contract",
                dry_run=True,
                external_id=external_id,
                docuseal_payload=payload,
            )

        client = _get_docuseal_client(settings)
        try:
            response = client.create_submission(payload)
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"DocuSeal request failed: {exc}") from exc

        return _submission_response("credit_contract", external_id, dry_run=False, docuseal_response=response)

    @app.post("/api/webhooks/docuseal", response_model=DocuSealWebhookResponse)
    def docuseal_webhook(
        body: dict[str, Any],
        settings: Annotated[DocumentIntegrationSettings, Depends(_get_document_settings)],
        _: None = Depends(_require_docuseal_webhook_token),
    ) -> DocuSealWebhookResponse:
        del settings
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        submission = data.get("submission") if isinstance(data.get("submission"), dict) else {}
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        variables = submission.get("variables") if isinstance(submission.get("variables"), dict) else {}
        documents = data.get("documents") if isinstance(data.get("documents"), list) else []

        document_type = _text(metadata.get("document_type")) or _text(variables.get("document_type"))
        clickup_task_id = _text(metadata.get("clickup_task_id")) or _text(variables.get("clickup_task_id"))
        event_type = _text(body.get("event_type"))
        action = "record_completion"
        if document_type == "credit_contract" and clickup_task_id:
            action = "record_completion_and_update_clickup"
        elif event_type and event_type not in {"form.completed", "submission.completed"}:
            action = "ignore_non_completion_event"

        return DocuSealWebhookResponse(
            accepted=True,
            event_type=event_type,
            document_type=document_type,
            submission_id=_int_value(submission.get("id") or data.get("submission_id")),
            submitter_id=_int_value(data.get("id")),
            external_id=_text(data.get("external_id")),
            clickup_task_id=clickup_task_id,
            documents_count=len(documents),
            action=action,
        )


def _get_document_settings() -> DocumentIntegrationSettings:
    load_dotenv()
    return DocumentIntegrationSettings.from_env()


def _get_docuseal_client(settings: DocumentIntegrationSettings) -> DocuSealClient:
    try:
        return DocuSealClient(settings)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _require_operator_auth(
    authorization: Annotated[str | None, Header()] = None,
    x_trigger_token: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query()] = None,
) -> None:
    from shipment_sync.api import _require_operator_auth as require_operator_auth

    require_operator_auth(authorization=authorization, x_trigger_token=x_trigger_token, token=token)


def _require_docuseal_webhook_token(
    settings: Annotated[DocumentIntegrationSettings, Depends(_get_document_settings)],
    authorization: Annotated[str | None, Header()] = None,
    x_docuseal_webhook_token: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query()] = None,
) -> None:
    expected = settings.docuseal_webhook_token
    if not expected:
        return
    provided = _extract_bearer_token(authorization) or x_docuseal_webhook_token or token
    if not provided:
        raise HTTPException(status_code=401, detail="Missing DocuSeal webhook token")
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid DocuSeal webhook token")


def _serialize_requirements(report: DocumentIntegrationConfigReport) -> DocumentRequirementsResponse:
    return DocumentRequirementsResponse(
        configured=report.configured,
        live_ready=report.live_ready,
        required_items=report.required_items,
        missing_required_items=report.missing_required_items,
        recommended_items=report.recommended_items,
        missing_recommended_items=report.missing_recommended_items,
        notes=report.notes,
    )


def _missing_nda_fields(body: NdaIssueRequest, settings: DocumentIntegrationSettings) -> list[str]:
    missing: list[str] = []
    if not body.counterparty.company_name.strip():
        missing.append("counterparty.company_name")
    if not body.counterparty_signer.name.strip():
        missing.append("counterparty_signer.name")
    if not body.counterparty_signer.email.strip():
        missing.append("counterparty_signer.email")
    signer = body.mtm_signer
    if signer is None:
        if not settings.mtm_default_signer_name:
            missing.append("mtm_signer.name or MTM_NDA_SIGNER_NAME")
        if not settings.mtm_default_signer_email:
            missing.append("mtm_signer.email or MTM_NDA_SIGNER_EMAIL")
    else:
        if not signer.name.strip():
            missing.append("mtm_signer.name")
        if not signer.email.strip():
            missing.append("mtm_signer.email")
    return missing


def _build_nda_submission_payload(
    body: NdaIssueRequest,
    settings: DocumentIntegrationSettings,
    external_id: str,
) -> dict[str, Any]:
    effective_date = (body.effective_date or date.today()).isoformat()
    mtm_signer = body.mtm_signer or DocumentSigner(
        name=settings.mtm_default_signer_name or "",
        email=settings.mtm_default_signer_email or "",
        title=settings.mtm_default_signer_title,
    )
    send_email = settings.docuseal_send_email_default if body.send_email is None else body.send_email
    metadata = {
        "document_type": "nda",
        "external_reference": body.external_reference,
        **(body.metadata or {}),
    }
    variables = _drop_none(
        {
            "document_type": "nda",
            "effective_date": effective_date,
            "disclosing_party_company_name": settings.mtm_company_name,
            "disclosing_party_company_address": settings.mtm_company_address,
            "disclosing_party_tax_id": settings.mtm_company_tax_id,
            "receiving_party_company_name": body.counterparty.company_name,
            "receiving_party_company_address": body.counterparty.company_address,
            "receiving_party_tax_id": body.counterparty.company_tax_id,
            "mtm_signer_name": mtm_signer.name,
            "mtm_signer_title": mtm_signer.title,
            "mtm_signer_email": mtm_signer.email,
            "counterparty_signer_name": body.counterparty_signer.name,
            "counterparty_signer_title": body.counterparty_signer.title,
            "counterparty_signer_email": body.counterparty_signer.email,
        }
    )
    shared_values = {
        "Effective Date": effective_date,
        "Disclosing Party Company Name": settings.mtm_company_name,
        "Disclosing Party Company Address": settings.mtm_company_address,
        "Disclosing Party Tax ID": settings.mtm_company_tax_id,
        "Receiving Party Company Name": body.counterparty.company_name,
        "Receiving Party Company Address": body.counterparty.company_address,
        "Receiving Party Tax ID": body.counterparty.company_tax_id,
    }

    return _drop_none(
        {
            "template_id": settings.docuseal_nda_template_id,
            "send_email": send_email,
            "order": "preserved",
            "variables": variables,
            "submitters": [
                _drop_none(
                    {
                        "role": settings.nda_mtm_role,
                        "name": mtm_signer.name,
                        "email": mtm_signer.email,
                        "phone": mtm_signer.phone,
                        "external_id": f"{external_id}:mtm",
                        "metadata": metadata,
                        "values": _drop_none(
                            {
                                **shared_values,
                                "Disclosing Party Signer Name": mtm_signer.name,
                                "Disclosing Party Signer Title": mtm_signer.title,
                                "Disclosing Party Signer Email": mtm_signer.email,
                            }
                        ),
                    }
                ),
                _drop_none(
                    {
                        "role": settings.nda_counterparty_role,
                        "name": body.counterparty_signer.name,
                        "email": body.counterparty_signer.email,
                        "phone": body.counterparty_signer.phone,
                        "external_id": f"{external_id}:counterparty",
                        "metadata": metadata,
                        "values": _drop_none(
                            {
                                **shared_values,
                                "Receiving Party Signer Name": body.counterparty_signer.name,
                                "Receiving Party Signer Title": body.counterparty_signer.title,
                                "Receiving Party Signer Email": body.counterparty_signer.email,
                            }
                        ),
                    }
                ),
            ],
        }
    )


def _normalize_credit_contract_request(body: CreditContractTriggerRequest) -> CreditContractTriggerRequest:
    payload = body.payload or {}
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    history_items = payload.get("history_items") if isinstance(payload.get("history_items"), list) else []
    first_history = history_items[0] if history_items and isinstance(history_items[0], dict) else {}

    return CreditContractTriggerRequest(
        task_id=body.task_id or _text(task.get("id")) or _text(payload.get("task_id")) or _text(first_history.get("task_id")),
        task_url=body.task_url or _text(task.get("url")) or _text(payload.get("task_url")),
        customer_company_name=body.customer_company_name or _text(payload.get("customer_company_name")),
        signer_name=body.signer_name or _text(payload.get("signer_name")),
        signer_email=body.signer_email or _text(payload.get("signer_email")),
        external_reference=body.external_reference or _text(payload.get("external_reference")),
        send_email=body.send_email,
        dry_run=body.dry_run,
        payload=payload,
    )


def _missing_credit_contract_fields(
    body: CreditContractTriggerRequest,
    settings: DocumentIntegrationSettings,
) -> list[str]:
    missing: list[str] = []
    if not settings.docuseal_credit_contract_template_id:
        missing.append("DOCUSEAL_CREDIT_CONTRACT_TEMPLATE_ID")
    if not _text(body.task_id):
        missing.append("task_id")
    if not _text(body.customer_company_name):
        missing.append("customer_company_name")
    if not _text(body.signer_name):
        missing.append("signer_name")
    if not _text(body.signer_email):
        missing.append("signer_email")
    return missing


def _build_credit_contract_submission_payload(
    body: CreditContractTriggerRequest,
    settings: DocumentIntegrationSettings,
    external_id: str,
) -> dict[str, Any]:
    send_email = settings.docuseal_send_email_default if body.send_email is None else body.send_email
    metadata = {
        "document_type": "credit_contract",
        "clickup_task_id": body.task_id,
        "clickup_task_url": body.task_url,
        "external_reference": body.external_reference,
    }
    variables = _drop_none(
        {
            "document_type": "credit_contract",
            "clickup_task_id": body.task_id,
            "clickup_task_url": body.task_url,
            "customer_company_name": body.customer_company_name,
            "customer_signer_name": body.signer_name,
            "customer_signer_email": body.signer_email,
        }
    )

    return _drop_none(
        {
            "template_id": settings.docuseal_credit_contract_template_id,
            "send_email": send_email,
            "order": "preserved",
            "variables": variables,
            "submitters": [
                _drop_none(
                    {
                        "role": settings.credit_contract_signer_role,
                        "name": body.signer_name,
                        "email": body.signer_email,
                        "external_id": external_id,
                        "metadata": _drop_none(metadata),
                        "values": _drop_none(
                            {
                                "Customer Company Name": body.customer_company_name,
                                "Customer Signer Name": body.signer_name,
                                "Customer Signer Email": body.signer_email,
                                "ClickUp Task ID": body.task_id,
                            }
                        ),
                    }
                )
            ],
        }
    )


def _submission_response(
    document_type: str,
    external_id: str,
    *,
    dry_run: bool,
    docuseal_response: Any,
) -> DocumentSubmissionResponse:
    submitters = docuseal_response if isinstance(docuseal_response, list) else docuseal_response.get("submitters", [])
    submitter_ids = [_id for item in submitters if (_id := _int_value(item.get("id")))]
    signing_urls = [
        url
        for item in submitters
        for url in [_text(item.get("embed_src")) or _text(item.get("url"))]
        if url
    ]
    submission_id = None
    if isinstance(docuseal_response, dict):
        submission_id = _int_value(docuseal_response.get("id"))
    if submission_id is None and submitters:
        submission_id = _int_value(submitters[0].get("submission_id"))
    return DocumentSubmissionResponse(
        document_type=document_type,
        dry_run=dry_run,
        external_id=external_id,
        docuseal_submission_id=submission_id,
        docuseal_submitter_ids=submitter_ids,
        signing_urls=signing_urls,
        docuseal_response=docuseal_response,
    )


def _external_id(document_type: str, reference: str | None, fallback: str | None) -> str:
    raw = reference or fallback or str(uuid4())
    slug = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", raw.strip()).strip("-").lower()
    return f"{document_type}:{slug or uuid4()}"


def _drop_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return authorization.strip() or None
