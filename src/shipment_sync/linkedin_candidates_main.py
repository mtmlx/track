from __future__ import annotations

from .terminal import terminal_safe_text

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .clickup_candidates_client import ClickUpCandidatesClient
from .linkedin_candidates_config import LinkedInCandidateSettings, load_criteria
from .linkedin_candidates_csv_source import LinkedInCsvSource
from .linkedin_candidates_models import CandidateProfile
from .linkedin_candidates_source import GoogleCustomSearchLinkedInSource, merge_job_candidates
from .linkedin_candidates_sync import sync_candidates_to_clickup

DEFAULT_CRITERIA_FILE = "config/local/linkedin_criteria.json"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Source LinkedIn profile links using job criteria and sync candidates to ClickUp"
    )
    parser.add_argument(
        "--criteria-file",
        default=DEFAULT_CRITERIA_FILE,
        help=(
            "Path to JSON criteria file "
            f"(default: {DEFAULT_CRITERIA_FILE}; also falls back to legacy root file if present)"
        ),
    )
    parser.add_argument(
        "--input-csv",
        help="Path to your LinkedIn export CSV. When set, Google CSE is not used.",
    )
    parser.add_argument(
        "--job",
        action="append",
        help="Filter by job id or job name token (repeatable)",
    )
    parser.add_argument(
        "--max-results-per-job",
        type=int,
        default=0,
        help="Override max candidate results per job (0 keeps per-job config)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview candidates and ClickUp actions without creating tasks",
    )
    parser.add_argument(
        "--inspect-fields",
        action="store_true",
        help="List custom fields in CLICKUP_CANDIDATES_LIST_ID and exit",
    )
    parser.add_argument(
        "--print-queries",
        action="store_true",
        help="Print generated search queries per job",
    )
    parser.add_argument(
        "--output-json",
        help="Optional path to write merged candidate output JSON for review",
    )
    args = parser.parse_args()

    load_dotenv()
    csv_mode = bool(args.input_csv)
    require_clickup = args.inspect_fields or not args.dry_run
    settings = LinkedInCandidateSettings.from_env(
        require_clickup=require_clickup,
        require_google_cse=(not args.inspect_fields and not csv_mode),
    )

    clickup_client = None
    if require_clickup or _has_clickup_credentials():
        if settings.clickup_api_token and settings.clickup_candidates_list_id:
            clickup_client = ClickUpCandidatesClient(settings)
    if args.inspect_fields:
        fields = clickup_client.list_custom_fields() if clickup_client else []
        if not fields:
            print("No custom fields found for CLICKUP_CANDIDATES_LIST_ID.")
            return
        print(terminal_safe_text(f"Found {len(fields)} custom fields:"))
        for field in fields:
            print(terminal_safe_text(f"- id={field['id']} | name={field['name']} | type={field['type']}"))
        return

    jobs = load_criteria(
        args.criteria_file,
        default_max_results=settings.candidate_default_max_results,
        filters=args.job,
    )
    source = LinkedInCsvSource(args.input_csv) if csv_mode else GoogleCustomSearchLinkedInSource(settings)

    job_results = []
    for job in jobs:
        result = source.search_job(
            job,
            max_results_override=args.max_results_per_job if args.max_results_per_job > 0 else None,
        )
        job_results.append(result)

    if args.print_queries:
        print(terminal_safe_text("Generated queries:" if not csv_mode else "Data sources:"))
        for result in job_results:
            print(terminal_safe_text(f"- {result.job.job_name}"))
            for query in result.queries:
                print(terminal_safe_text(f"  - {query}"))

    candidates = merge_job_candidates(job_results)
    _print_candidate_preview(candidates)

    if args.output_json:
        output_path = Path(args.output_json)
        payload = [candidate.to_dict() for candidate in candidates]
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(terminal_safe_text(f"Wrote {len(candidates)} candidates to {output_path}"))

    if args.dry_run:
        if clickup_client:
            stats = sync_candidates_to_clickup(clickup_client, candidates, dry_run=True)
            print(
                terminal_safe_text("Dry run complete. "
                f"Candidates: {stats.total_candidates}, "
                f"To create: {stats.to_create}, "
                f"Already existing: {stats.skipped_existing}")
            )
        else:
            print(terminal_safe_text(f"Dry run complete. Candidates found: {len(candidates)}"))
        return

    if clickup_client is None:
        raise ValueError("ClickUp client is required for live sync")

    stats = sync_candidates_to_clickup(clickup_client, candidates, dry_run=False)
    print(
        terminal_safe_text("LinkedIn candidate sync complete. "
        f"Candidates: {stats.total_candidates}, "
        f"To create: {stats.to_create}, "
        f"Created: {stats.created}, "
        f"Already existing: {stats.skipped_existing}, "
        f"Errors: {len(stats.errors)}")
    )
    if stats.errors:
        for err in stats.errors:
            print(terminal_safe_text(f"- {err}"))


def _print_candidate_preview(candidates: list[CandidateProfile]) -> None:
    print(terminal_safe_text(f"Candidates found: {len(candidates)}"))
    for index, candidate in enumerate(candidates, start=1):
        roles = ", ".join(candidate.job_names) if candidate.job_names else "n/a"
        source_query = candidate.source_queries[0] if candidate.source_queries else "n/a"
        print(
            terminal_safe_text(f"{index}. score={candidate.score} | name={candidate.full_name} | "
            f"roles={roles} | linkedin={candidate.linkedin_url}")
        )
        print(terminal_safe_text(f"   source={source_query}"))
        if candidate.headline:
            print(terminal_safe_text(f"   headline={candidate.headline}"))


def _has_clickup_credentials() -> bool:
    return bool(os.getenv("CLICKUP_API_TOKEN") and os.getenv("CLICKUP_CANDIDATES_LIST_ID"))


if __name__ == "__main__":
    main()
