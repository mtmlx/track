from __future__ import annotations

import argparse
import csv
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests
from dotenv import load_dotenv

from .clickup_fields_main import _spreadsheet_safe_cell
from .linkedin_candidates_config import load_criteria
from .linkedin_candidates_models import JobCriteria
from .project_paths import REPO_ROOT, local_config_path, resolve_optional_existing_file

_TRUTHY = {"1", "true", "yes", "y", "approved"}
_PROFILE_PATH_RE = re.compile(r"^/(in|pub)/[^/?#]+")
DEFAULT_CAMPAIGN_FILE = "config/local/linkedin_copilot_campaign.json"
DEFAULT_OUTPUT_DIR = "artifacts/output/linkedin_copilot"


@dataclass
class FilterConfig:
    min_score: int = 0
    connected_within_days: int = 0
    include_titles: list[str] = field(default_factory=list)
    exclude_titles: list[str] = field(default_factory=list)
    include_companies: list[str] = field(default_factory=list)
    exclude_companies: list[str] = field(default_factory=list)
    include_unknown_connection_date: bool = True


@dataclass
class AIConfig:
    enabled: bool = False
    model: str = "gpt-4.1-mini"
    max_output_tokens: int = 220
    timeout_seconds: int = 45


@dataclass
class CampaignConfig:
    campaign_name: str = "LinkedIn Copilot Campaign"
    goal: str = "reconnect with relevant contacts and start valuable conversations"
    audience_notes: str = ""
    tone: str = "professional and warm"
    cta: str = "Would you be open to a short conversation next week?"
    comment_focus: str = "practical execution"
    max_drafts: int = 25
    filters: FilterConfig = field(default_factory=FilterConfig)
    ai: AIConfig = field(default_factory=AIConfig)


@dataclass
class ConnectionRecord:
    contact_id: str
    first_name: str
    last_name: str
    full_name: str
    linkedin_url: str
    email: str | None
    company: str | None
    position: str | None
    connected_on: str | None


@dataclass
class ScoredConnection:
    record: ConnectionRecord
    score: int
    match_summary: str
    job_matches: list[str]


@dataclass
class FilteredOutRecord:
    id: str
    full_name: str
    linkedin_url: str
    score: int
    reason: str
    company: str | None
    position: str | None
    connected_on: str | None


@dataclass
class DraftRecord:
    id: str
    full_name: str
    linkedin_url: str
    score: int
    match_summary: str
    job_matches: list[str]
    company: str | None
    position: str | None
    connected_on: str | None
    message_draft: str
    comment_draft: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class OpenAIDraftGenerator:
    def __init__(self, *, api_key: str, model: str, timeout_seconds: int, max_output_tokens: int):
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens

    def generate(
        self,
        *,
        record: ConnectionRecord,
        campaign: CampaignConfig,
        summary: str,
        template_message: str,
        template_comment: str,
    ) -> tuple[str, str] | None:
        user_payload = {
            "campaign": {
                "name": campaign.campaign_name,
                "goal": campaign.goal,
                "tone": campaign.tone,
                "audience_notes": campaign.audience_notes,
                "cta": campaign.cta,
                "comment_focus": campaign.comment_focus,
            },
            "contact": {
                "full_name": record.full_name,
                "first_name": record.first_name,
                "company": record.company,
                "position": record.position,
                "connected_on": record.connected_on,
            },
            "match_summary": summary,
            "template_message": template_message,
            "template_comment": template_comment,
        }

        system_prompt = (
            "You write concise and professional LinkedIn outreach drafts. "
            "Return strict JSON with keys 'message' and 'comment'. "
            "No markdown, no extra keys, no placeholders, no hashtags, no emoji. "
            "Keep message <= 90 words and comment <= 45 words."
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Create personalized drafts using this JSON input. "
                        "Keep it natural and specific but conservative.\n"
                        f"{json.dumps(user_payload, ensure_ascii=True)}"
                    ),
                },
            ],
            "temperature": 0.6,
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_output_tokens,
        }

        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()

        content = _extract_chat_content(body)
        if not content:
            return None

        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return None

        message = _collapse_ws(_text(parsed.get("message")))
        comment = _collapse_ws(_text(parsed.get("comment")))
        if not message or not comment:
            return None
        return message, comment


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a LinkedIn outreach copilot queue from your own connections CSV. "
            "This tool prepares drafts and approval files only; it does not post or send actions automatically."
        )
    )
    parser.add_argument("--connections-csv", required=True, help="Path to exported LinkedIn connections CSV")
    parser.add_argument(
        "--campaign-file",
        default=DEFAULT_CAMPAIGN_FILE,
        help=(
            "Optional campaign JSON with tone/goal settings "
            f"(default: {DEFAULT_CAMPAIGN_FILE}; falls back to the legacy root file and then built-in defaults)"
        ),
    )
    parser.add_argument(
        "--criteria-file",
        default="",
        help="Optional criteria JSON (same format as linkedin-candidates) used to score/prioritize contacts",
    )
    parser.add_argument("--job", action="append", help="Filter criteria jobs by id/name token (repeatable)")
    parser.add_argument(
        "--top",
        type=int,
        default=0,
        help="Limit number of generated drafts (0 uses campaign max_drafts)",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for generated review artifacts",
    )
    parser.add_argument(
        "--approval-file",
        default="",
        help=(
            "Optional path to a reviewed queue CSV. "
            "When provided, an approved-only send queue is generated."
        ),
    )
    parser.add_argument("--use-ai-drafts", action="store_true", help="Generate drafts with OpenAI (optional)")
    parser.add_argument("--ai-model", default="", help="Override OpenAI model for AI drafts")
    parser.add_argument("--min-score", type=int, default=-1, help="Override minimum score filter")
    parser.add_argument(
        "--connected-within-days",
        type=int,
        default=-1,
        help="Only include contacts connected in the last N days (0 disables)",
    )
    parser.add_argument("--include-title", action="append", help="Keep only contacts whose title contains token")
    parser.add_argument("--exclude-title", action="append", help="Exclude contacts whose title contains token")
    parser.add_argument("--include-company", action="append", help="Keep only contacts whose company contains token")
    parser.add_argument("--exclude-company", action="append", help="Exclude contacts whose company contains token")
    parser.add_argument(
        "--exclude-unknown-connection-date",
        action="store_true",
        help="Exclude rows with missing/unparseable connected_on when connected-within-days is active",
    )
    args = parser.parse_args()

    load_dotenv()

    campaign = _load_campaign_config(args.campaign_file)
    _apply_cli_overrides(campaign, args)

    jobs: list[JobCriteria] = []
    if args.criteria_file:
        jobs = load_criteria(
            args.criteria_file,
            default_max_results=max(1, campaign.max_drafts),
            filters=args.job,
        )

    connections = _read_connections_csv(Path(args.connections_csv))
    scored, filtered_out = _rank_connections(connections=connections, jobs=jobs, filters=campaign.filters)
    selected_scored = scored[: campaign.max_drafts]

    ai_generator, ai_warning = _build_ai_generator(campaign.ai)
    drafts, ai_generated, ai_failed = _build_drafts(
        selected=selected_scored,
        campaign=campaign,
        ai_generator=ai_generator,
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    json_path = output_dir / f"drafts-{ts}.json"
    md_path = output_dir / f"drafts-{ts}.md"
    review_csv_path = output_dir / f"review-queue-{ts}.csv"

    json_path.write_text(json.dumps([row.to_dict() for row in drafts], indent=2), encoding="utf-8")
    md_path.write_text(_build_markdown(selected=drafts, campaign=campaign), encoding="utf-8")
    _write_review_csv(selected=drafts, destination=review_csv_path)

    filtered_csv_path: Path | None = None
    if filtered_out:
        filtered_csv_path = output_dir / f"filtered-out-{ts}.csv"
        _write_filtered_csv(records=filtered_out, destination=filtered_csv_path)

    print(f"Connections loaded: {len(connections)}")
    print(f"Filtered out: {len(filtered_out)}")
    print(f"Eligible after filters: {len(scored)}")
    print(f"Drafts prepared: {len(drafts)}")
    print(f"Campaign: {campaign.campaign_name}")
    print(f"JSON draft output: {json_path}")
    print(f"Markdown review: {md_path}")
    print(f"Approval queue CSV: {review_csv_path}")
    if filtered_csv_path:
        print(f"Filtered rows CSV: {filtered_csv_path}")

    if campaign.ai.enabled:
        print(f"AI drafts requested: yes (model={campaign.ai.model})")
        print(f"AI drafts generated: {ai_generated}; AI fallbacks: {ai_failed}")
        if ai_warning:
            print(f"AI note: {ai_warning}")

    print("No LinkedIn actions were executed. Review and approve drafts manually before sending.")

    if args.approval_file:
        approved_queue = _build_approved_queue(Path(args.approval_file), output_dir=output_dir, timestamp=ts)
        print(f"Approved-only queue: {approved_queue}")


def _load_campaign_config(path: str) -> CampaignConfig:
    config = CampaignConfig(
        ai=AIConfig(
            enabled=_env_bool("LINKEDIN_COPILOT_USE_AI_DRAFTS", default=False),
            model=os.getenv("LINKEDIN_COPILOT_OPENAI_MODEL", "gpt-4.1-mini"),
            timeout_seconds=_env_int("LINKEDIN_COPILOT_OPENAI_TIMEOUT_SECONDS", default=45, minimum=5),
            max_output_tokens=_env_int("LINKEDIN_COPILOT_OPENAI_MAX_OUTPUT_TOKENS", default=220, minimum=64),
        )
    )
    config_path = resolve_optional_existing_file(
        path,
        project_candidates=(
            local_config_path("linkedin_copilot_campaign.json"),
            REPO_ROOT / "linkedin_copilot_campaign.json",
        ),
        allow_missing_explicit=True,
    )
    if config_path is None:
        return config

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid JSON in campaign file: {config_path}") from exc

    if not isinstance(raw, dict):
        raise ValueError("Campaign file must be a JSON object")

    name = _text(raw.get("campaign_name"))
    goal = _text(raw.get("goal"))
    audience_notes = _text(raw.get("audience_notes"))
    tone = _text(raw.get("tone"))
    cta = _text(raw.get("cta"))
    comment_focus = _text(raw.get("comment_focus"))
    max_drafts = _coerce_int(raw.get("max_drafts"), default=config.max_drafts, minimum=1)

    filters = FilterConfig(
        min_score=_coerce_int(raw.get("min_score"), default=config.filters.min_score, minimum=0),
        connected_within_days=_coerce_int(
            raw.get("connected_within_days"), default=config.filters.connected_within_days, minimum=0
        ),
        include_titles=_to_text_list(raw.get("include_titles")),
        exclude_titles=_to_text_list(raw.get("exclude_titles")),
        include_companies=_to_text_list(raw.get("include_companies")),
        exclude_companies=_to_text_list(raw.get("exclude_companies")),
        include_unknown_connection_date=_coerce_bool(
            raw.get("include_unknown_connection_date"), default=config.filters.include_unknown_connection_date
        ),
    )

    ai = AIConfig(
        enabled=_coerce_bool(raw.get("use_ai_drafts"), default=config.ai.enabled),
        model=_text(raw.get("ai_model")) or config.ai.model,
        timeout_seconds=_coerce_int(raw.get("ai_timeout_seconds"), default=config.ai.timeout_seconds, minimum=5),
        max_output_tokens=_coerce_int(raw.get("ai_max_output_tokens"), default=config.ai.max_output_tokens, minimum=64),
    )

    return CampaignConfig(
        campaign_name=name or config.campaign_name,
        goal=goal or config.goal,
        audience_notes=audience_notes,
        tone=tone or config.tone,
        cta=cta or config.cta,
        comment_focus=comment_focus or config.comment_focus,
        max_drafts=max_drafts,
        filters=filters,
        ai=ai,
    )


def _apply_cli_overrides(campaign: CampaignConfig, args: argparse.Namespace) -> None:
    if args.top > 0:
        campaign.max_drafts = args.top

    if args.use_ai_drafts:
        campaign.ai.enabled = True
    if args.ai_model:
        campaign.ai.model = args.ai_model.strip()

    if args.min_score >= 0:
        campaign.filters.min_score = args.min_score
    if args.connected_within_days >= 0:
        campaign.filters.connected_within_days = args.connected_within_days

    if args.include_title:
        campaign.filters.include_titles.extend(token.strip() for token in args.include_title if token.strip())
    if args.exclude_title:
        campaign.filters.exclude_titles.extend(token.strip() for token in args.exclude_title if token.strip())
    if args.include_company:
        campaign.filters.include_companies.extend(token.strip() for token in args.include_company if token.strip())
    if args.exclude_company:
        campaign.filters.exclude_companies.extend(token.strip() for token in args.exclude_company if token.strip())

    if args.exclude_unknown_connection_date:
        campaign.filters.include_unknown_connection_date = False


def _build_ai_generator(ai_config: AIConfig) -> tuple[OpenAIDraftGenerator | None, str | None]:
    if not ai_config.enabled:
        return None, None

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, "OPENAI_API_KEY is not set; using template drafts."

    return (
        OpenAIDraftGenerator(
            api_key=api_key,
            model=ai_config.model,
            timeout_seconds=ai_config.timeout_seconds,
            max_output_tokens=ai_config.max_output_tokens,
        ),
        None,
    )


def _read_connections_csv(path: Path) -> list[ConnectionRecord]:
    if not path.exists():
        raise ValueError(f"Connections CSV not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"Connections CSV is missing header row: {path}")

        rows: list[ConnectionRecord] = []
        for row_idx, row in enumerate(reader, start=1):
            if not isinstance(row, dict):
                continue
            normalized: dict[str, str] = {}
            for key, value in row.items():
                if key is None:
                    continue
                normalized[_normalize_token(key)] = str(value or "").strip()

            url = _normalize_linkedin_url(
                _pick(normalized, ["profileurl", "url", "linkedinurl", "publicprofileurl"])
            )
            if not url:
                continue

            first_name = _pick(normalized, ["firstname", "first_name"]) or ""
            last_name = _pick(normalized, ["lastname", "last_name"]) or ""
            full_name = " ".join(part for part in [first_name, last_name] if part).strip() or "Unknown"

            rows.append(
                ConnectionRecord(
                    contact_id=f"c{row_idx}",
                    first_name=first_name,
                    last_name=last_name,
                    full_name=full_name,
                    linkedin_url=url,
                    email=_pick(normalized, ["emailaddress", "email"]),
                    company=_pick(normalized, ["company", "currentcompany"]),
                    position=_pick(normalized, ["position", "title", "headline"]),
                    connected_on=_pick(normalized, ["connectedon", "connected_on", "connectiondate"]),
                )
            )

    if not rows:
        raise ValueError(f"No valid LinkedIn profile URLs found in CSV: {path}")
    return rows


def _rank_connections(
    *,
    connections: list[ConnectionRecord],
    jobs: list[JobCriteria],
    filters: FilterConfig,
) -> tuple[list[ScoredConnection], list[FilteredOutRecord]]:
    kept: list[ScoredConnection] = []
    filtered: list[FilteredOutRecord] = []

    for record in connections:
        score, summary, job_matches = _score_connection(record=record, jobs=jobs)
        reason = _filter_reason(record=record, score=score, filters=filters)
        if reason:
            filtered.append(
                FilteredOutRecord(
                    id=record.contact_id,
                    full_name=record.full_name,
                    linkedin_url=record.linkedin_url,
                    score=score,
                    reason=reason,
                    company=record.company,
                    position=record.position,
                    connected_on=record.connected_on,
                )
            )
            continue

        kept.append(
            ScoredConnection(
                record=record,
                score=score,
                match_summary=summary,
                job_matches=job_matches,
            )
        )

    kept.sort(
        key=lambda row: (
            -row.score,
            row.record.full_name.lower(),
            row.record.linkedin_url,
        )
    )
    return kept, filtered


def _build_drafts(
    *,
    selected: list[ScoredConnection],
    campaign: CampaignConfig,
    ai_generator: OpenAIDraftGenerator | None,
) -> tuple[list[DraftRecord], int, int]:
    drafts: list[DraftRecord] = []
    ai_generated = 0
    ai_failed = 0

    for scored in selected:
        template_message = _build_message_draft(
            record=scored.record,
            campaign=campaign,
            summary=scored.match_summary,
        )
        template_comment = _build_comment_draft(record=scored.record, campaign=campaign)

        message = template_message
        comment = template_comment

        if ai_generator is not None:
            try:
                ai_result = ai_generator.generate(
                    record=scored.record,
                    campaign=campaign,
                    summary=scored.match_summary,
                    template_message=template_message,
                    template_comment=template_comment,
                )
            except Exception:
                ai_result = None

            if ai_result is None:
                ai_failed += 1
            else:
                ai_generated += 1
                message, comment = ai_result

        drafts.append(
            DraftRecord(
                id=scored.record.contact_id,
                full_name=scored.record.full_name,
                linkedin_url=scored.record.linkedin_url,
                score=scored.score,
                match_summary=scored.match_summary,
                job_matches=scored.job_matches,
                company=scored.record.company,
                position=scored.record.position,
                connected_on=scored.record.connected_on,
                message_draft=message,
                comment_draft=comment,
            )
        )

    return drafts, ai_generated, ai_failed


def _score_connection(record: ConnectionRecord, jobs: list[JobCriteria]) -> tuple[int, str, list[str]]:
    searchable = " ".join(
        piece
        for piece in [record.full_name, record.position or "", record.company or "", record.email or ""]
        if piece
    ).lower()

    reasons: list[str] = []
    base_score = 30
    if record.position:
        base_score += 6
        reasons.append("has role/title")
    if record.company:
        base_score += 4
        reasons.append("has company data")

    recency_bonus, recency_reason = _recency_bonus(record.connected_on)
    base_score += recency_bonus
    if recency_reason:
        reasons.append(recency_reason)

    matched_jobs: list[str] = []
    best_job_boost: int | None = None
    best_job_reasons: list[str] = []

    for job in jobs:
        title_hits = [token for token in job.titles if token.lower() in searchable]
        skill_hits = [token for token in job.skills if token.lower() in searchable]
        include_hits = [token for token in job.include_keywords if token.lower() in searchable]
        exclude_hits = [token for token in job.exclude_keywords if token.lower() in searchable]

        boost = 0
        boost += len(title_hits) * 12
        boost += len(skill_hits) * 6
        boost += len(include_hits) * 4
        boost -= len(exclude_hits) * 22

        if title_hits or skill_hits or include_hits:
            matched_jobs.append(job.job_name)

        if best_job_boost is None or boost > best_job_boost:
            best_job_boost = boost
            best_job_reasons = []
            if title_hits:
                best_job_reasons.append(f"title match ({len(title_hits)})")
            if skill_hits:
                best_job_reasons.append(f"skill match ({len(skill_hits)})")
            if include_hits:
                best_job_reasons.append(f"keyword match ({len(include_hits)})")
            if exclude_hits:
                best_job_reasons.append(f"exclude penalty ({len(exclude_hits)})")

    total = max(0, min(100, base_score + (best_job_boost or 0)))

    reasons.extend(best_job_reasons)
    if matched_jobs:
        reasons.append(f"matched jobs: {', '.join(sorted(set(matched_jobs)))}")

    summary = "; ".join(reasons) if reasons else "basic connection record"
    return total, summary, sorted(set(matched_jobs))


def _filter_reason(record: ConnectionRecord, score: int, filters: FilterConfig) -> str | None:
    reasons: list[str] = []

    if score < filters.min_score:
        reasons.append(f"score<{filters.min_score}")

    title_value = (record.position or "").lower()
    company_value = (record.company or "").lower()

    if filters.include_titles and not _matches_any(title_value, filters.include_titles):
        reasons.append("title not in include_titles")
    if filters.exclude_titles and _matches_any(title_value, filters.exclude_titles):
        reasons.append("title matched exclude_titles")

    if filters.include_companies and not _matches_any(company_value, filters.include_companies):
        reasons.append("company not in include_companies")
    if filters.exclude_companies and _matches_any(company_value, filters.exclude_companies):
        reasons.append("company matched exclude_companies")

    if filters.connected_within_days > 0:
        parsed = _parse_date(record.connected_on or "")
        if parsed is None:
            if not filters.include_unknown_connection_date:
                reasons.append("missing/unparseable connected_on")
        else:
            now = datetime.now(UTC).date()
            age_days = (now - parsed.date()).days
            if age_days > filters.connected_within_days:
                reasons.append(f"connected_on older than {filters.connected_within_days} days")

    if not reasons:
        return None
    return "; ".join(reasons)


def _matches_any(value: str, patterns: list[str]) -> bool:
    if not value:
        return False
    for pattern in patterns:
        if pattern.lower() in value:
            return True
    return False


def _recency_bonus(connected_on: str | None) -> tuple[int, str | None]:
    if not connected_on:
        return 0, None

    parsed = _parse_date(connected_on)
    if parsed is None:
        return 0, None

    now = datetime.now(UTC).date()
    days = (now - parsed.date()).days
    if days < 0:
        return 0, None
    if days <= 90:
        return 8, "recent connection (<=90d)"
    if days <= 365:
        return 4, "connected within last year"
    return 0, None


def _parse_date(raw: str) -> datetime | None:
    value = raw.strip()
    if not value:
        return None

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _build_message_draft(record: ConnectionRecord, campaign: CampaignConfig, summary: str) -> str:
    first_name = record.first_name or record.full_name.split()[0]
    role_part = _role_phrase(record.position, record.company)
    audience_part = f" {campaign.audience_notes.strip()}" if campaign.audience_notes.strip() else ""

    lines = [
        f"Hi {first_name},",
        f"Great to stay connected here. {role_part}",
        f"I'm running a {campaign.tone} outreach campaign to {campaign.goal}.{audience_part}",
        f"Why I thought of you: {summary}.",
        campaign.cta,
    ]
    return _collapse_ws(" ".join(line.strip() for line in lines if line.strip()))


def _build_comment_draft(record: ConnectionRecord, campaign: CampaignConfig) -> str:
    first_name = record.first_name or record.full_name.split()[0]
    company_part = f" at {record.company}" if record.company else ""
    return _collapse_ws(
        f"Great insight, {first_name}. The point about {campaign.comment_focus} really stands out{company_part}. "
        "I like how you made it practical instead of theoretical."
    )


def _safe_csv_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _spreadsheet_safe_cell(value) for key, value in row.items()}


def _write_review_csv(*, selected: list[DraftRecord], destination: Path) -> None:
    headers = [
        "id",
        "full_name",
        "linkedin_url",
        "score",
        "match_summary",
        "job_matches",
        "company",
        "position",
        "connected_on",
        "message_draft",
        "comment_draft",
        "approved",
        "edited_message",
        "edited_comment",
        "notes",
    ]

    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in selected:
            writer.writerow(_safe_csv_row(
                {
                    "id": row.id,
                    "full_name": row.full_name,
                    "linkedin_url": row.linkedin_url,
                    "score": row.score,
                    "match_summary": row.match_summary,
                    "job_matches": " | ".join(row.job_matches),
                    "company": row.company or "",
                    "position": row.position or "",
                    "connected_on": row.connected_on or "",
                    "message_draft": row.message_draft,
                    "comment_draft": row.comment_draft,
                    "approved": "",
                    "edited_message": "",
                    "edited_comment": "",
                    "notes": "",
                }
            ))


def _write_filtered_csv(*, records: list[FilteredOutRecord], destination: Path) -> None:
    headers = ["id", "full_name", "linkedin_url", "score", "reason", "company", "position", "connected_on"]
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in records:
            writer.writerow(_safe_csv_row(
                {
                    "id": row.id,
                    "full_name": row.full_name,
                    "linkedin_url": row.linkedin_url,
                    "score": row.score,
                    "reason": row.reason,
                    "company": row.company or "",
                    "position": row.position or "",
                    "connected_on": row.connected_on or "",
                }
            ))


def _build_approved_queue(approval_file: Path, *, output_dir: Path, timestamp: str) -> Path:
    if not approval_file.exists():
        raise ValueError(f"Approval CSV not found: {approval_file}")

    with approval_file.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"Approval CSV has no header row: {approval_file}")
        rows = list(reader)

    approved_rows: list[dict[str, str]] = []
    for row in rows:
        approved_raw = str((row or {}).get("approved") or "").strip().lower()
        if approved_raw not in _TRUTHY:
            continue
        edited_message = str((row or {}).get("edited_message") or "").strip()
        edited_comment = str((row or {}).get("edited_comment") or "").strip()
        message = edited_message or str((row or {}).get("message_draft") or "").strip()
        comment = edited_comment or str((row or {}).get("comment_draft") or "").strip()

        approved_rows.append(
            {
                "id": str((row or {}).get("id") or "").strip(),
                "full_name": str((row or {}).get("full_name") or "").strip(),
                "linkedin_url": str((row or {}).get("linkedin_url") or "").strip(),
                "message_to_send": message,
                "comment_to_post": comment,
                "notes": str((row or {}).get("notes") or "").strip(),
            }
        )

    destination = output_dir / f"approved-queue-{timestamp}.csv"
    headers = ["id", "full_name", "linkedin_url", "message_to_send", "comment_to_post", "notes"]
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(_safe_csv_row(row) for row in approved_rows)

    print(f"Approved rows: {len(approved_rows)}")
    return destination


def _build_markdown(*, selected: list[DraftRecord], campaign: CampaignConfig) -> str:
    lines = [
        f"# {campaign.campaign_name}",
        "",
        f"Goal: {campaign.goal}",
        f"Tone: {campaign.tone}",
        f"Review date (UTC): {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Draft Queue",
        "",
    ]

    for idx, row in enumerate(selected, start=1):
        lines.append(f"### {idx}. {row.full_name} (score: {row.score})")
        lines.append(f"- LinkedIn: {row.linkedin_url}")
        if row.position or row.company:
            lines.append(f"- Role: {row.position or 'n/a'} | Company: {row.company or 'n/a'}")
        if row.connected_on:
            lines.append(f"- Connected on: {row.connected_on}")
        lines.append(f"- Match summary: {row.match_summary}")
        lines.append(f"- Message draft: {row.message_draft}")
        lines.append(f"- Comment draft: {row.comment_draft}")
        lines.append("")

    lines.append("All actions remain manual: this output is for human review and approval.")
    return "\n".join(lines)


def _role_phrase(position: str | None, company: str | None) -> str:
    if position and company:
        return f"I noticed your work as {position} at {company}."
    if position:
        return f"I noticed your work as {position}."
    if company:
        return f"I noticed your work at {company}."
    return "I wanted to reconnect based on our network overlap."


def _extract_chat_content(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None

    content = message.get("content")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        if parts:
            return "\n".join(parts)
    return None


def _pick(row: dict[str, str], keys: list[str]) -> str | None:
    for key in keys:
        value = row.get(_normalize_token(key), "").strip()
        if value:
            return value
    return None


def _normalize_linkedin_url(raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    try:
        parsed = urlparse(raw_url.strip())
    except Exception:
        return None

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "linkedin.com":
        return None
    if not _PROFILE_PATH_RE.match(parsed.path):
        return None

    normalized = parsed._replace(
        scheme="https",
        netloc="www.linkedin.com",
        path=parsed.path.rstrip("/"),
        params="",
        query="",
        fragment="",
    )
    return urlunparse(normalized)


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.strip().lower())


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _to_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            cleaned = _text(item)
            if cleaned:
                items.append(cleaned)
        return items
    return []


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return _text(value).lower() in _TRUTHY


def _coerce_int(value: Any, *, default: int, minimum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return default
    return parsed if parsed >= minimum else default


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _env_int(name: str, *, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    return _coerce_int(raw, default=default, minimum=minimum)


if __name__ == "__main__":
    main()
