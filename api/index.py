from datetime import datetime
from difflib import SequenceMatcher
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from groq import Groq
import requests

try:
    from . import azure_devops
    from .prompt_builder import build_main_review_prompt, build_reflection_prompt
except ImportError:
    import azure_devops
    from prompt_builder import build_main_review_prompt, build_reflection_prompt

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = PROJECT_ROOT / "rules.txt"
DEBUG_DIR = PROJECT_ROOT / "debug"

# Load .env before reading environment variables.
load_dotenv(PROJECT_ROOT / ".env")

# ----- APP SETUP -----
app = Flask(__name__, template_folder=str(PROJECT_ROOT / "templates"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
ADO_ORG = os.getenv("ADO_ORG")
ADO_PROJECT = os.getenv("ADO_PROJECT")
ADO_REPO = os.getenv("ADO_REPO")
ADO_PAT = os.getenv("ADO_PAT")
ENABLE_REFLECTION = os.getenv("ENABLE_REFLECTION", "true").lower() == "true"
SUPPORTED_AZURE_EVENTS = {
    "git.pullrequest.created",
    "git.pullrequest.updated",
    # Temporary while the Azure service hook is still sending merged events for testing.
    # Remove this once the hook is switched to the correct PR created/updated events.
    "git.pullrequest.merged",
}
# ----- LOGGING CONFIG -----
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

logger.info("GROQ_API_KEY exists: %s", bool(GROQ_API_KEY))
logger.info(
    "Azure DevOps config exists: org=%s project=%s repo=%s pat=%s",
    bool(ADO_ORG),
    bool(ADO_PROJECT),
    bool(ADO_REPO),
    bool(ADO_PAT),
)
if not GROQ_API_KEY:
    error_message = "Missing GROQ_API_KEY or API_KEY environment variable. Set one in your .env file."
    logger.error(error_message)
    raise RuntimeError(error_message)

client = Groq(api_key=GROQ_API_KEY)
logger.info("Groq client initialized successfully")
logger.info("Reflection pass enabled: %s", ENABLE_REFLECTION)
LAST_RESULT: Dict[str, Any] = {}
NO_STRONG_BUG_TEXT = "No strong bug detected"
EXPECTED_FINDING_COLUMNS = (
    "Bug Title",
    "File",
    "Evidence",
    "Risk Type",
    "Impact",
    "Confidence",
)
SPECULATIVE_TERMS = {
    "could",
    "may",
    "might",
    "possibly",
    "potentially",
    "probably",
    "appears",
    "seems",
    "suggests",
    "likely",
}
USER_VISIBLE_IMPACT_TERMS = {
    "user",
    "save",
    "render",
    "display",
    "button",
    "icon",
    "screen",
    "popup",
    "wizard",
    "workflow",
    "grid",
    "treegrid",
    "click",
    "open",
    "close",
    "blank",
    "missing",
    "duplicate",
    "wrong",
    "fail",
    "error",
    "stale",
    "load",
    "blocked",
    "visible",
    "hidden",
}
CODE_TOKEN_STOPWORDS = {
    "file",
    "risk",
    "type",
    "impact",
    "confidence",
    "high",
    "medium",
    "low",
    "other",
}


# ----- HEALTH CHECK -----
@app.route("/healthz", methods=["GET"])
def health_check():
    logger.info("Health check triggered")
    return jsonify({"status": "ok"}), 200


def call_groq_review(prompt: str, temperature: float = 0.2) -> str:
    logger.info("Calling Groq API with model: %s", GROQ_MODEL)
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    result = response.choices[0].message.content.strip()
    logger.info("Groq response received (%d chars)", len(result))
    return result


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_repo_path(path: str) -> str:
    return azure_devops.normalize_repo_relative_path(str(path or "").strip())


def normalize_column_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_whitespace(value).lower())


def normalize_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalize_whitespace(value).lower()).strip()


def is_no_strong_bug_response(value: str) -> bool:
    return normalize_match_text(value) == normalize_match_text(NO_STRONG_BUG_TEXT)


def strip_markdown_cell(value: str) -> str:
    cleaned = normalize_whitespace(value.replace("<br>", " / ").replace("<br/>", " / "))
    cleaned = cleaned.strip("*_ ")
    if cleaned.startswith("`") and cleaned.endswith("`") and len(cleaned) > 1:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def split_markdown_row(line: str) -> List[str]:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []

    placeholder = "\uFFF0"
    escaped = stripped.replace(r"\|", placeholder)
    return [
        cell.replace(placeholder, "|").strip()
        for cell in escaped.strip("|").split("|")
    ]


def is_separator_row(cells: List[str]) -> bool:
    return bool(cells) and all(
        re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells
    )


def parse_markdown_findings(review_text: str) -> Tuple[List[Dict[str, str]], bool]:
    stripped = str(review_text or "").strip()
    if not stripped or is_no_strong_bug_response(stripped):
        return [], False

    table_lines = [
        line.strip()
        for line in stripped.splitlines()
        if line.strip().startswith("|")
    ]
    if len(table_lines) < 2:
        return [], True

    header_cells = split_markdown_row(table_lines[0])
    expected_headers = [normalize_column_name(name) for name in EXPECTED_FINDING_COLUMNS]
    if len(header_cells) < len(EXPECTED_FINDING_COLUMNS):
        return [], True
    if [normalize_column_name(cell) for cell in header_cells[: len(EXPECTED_FINDING_COLUMNS)]] != expected_headers:
        return [], True

    findings: List[Dict[str, str]] = []
    parse_error = False
    saw_separator = False

    for line in table_lines[1:]:
        cells = split_markdown_row(line)
        if not cells:
            continue
        if is_separator_row(cells):
            saw_separator = True
            continue
        if len(cells) < len(EXPECTED_FINDING_COLUMNS):
            parse_error = True
            continue
        if len(cells) > len(EXPECTED_FINDING_COLUMNS):
            cells = cells[: len(EXPECTED_FINDING_COLUMNS) - 1] + [" | ".join(cells[len(EXPECTED_FINDING_COLUMNS) - 1 :])]

        finding = {
            "bug_title": strip_markdown_cell(cells[0]),
            "file": normalize_repo_path(cells[1]),
            "evidence": strip_markdown_cell(cells[2]),
            "risk_type": strip_markdown_cell(cells[3]),
            "impact": strip_markdown_cell(cells[4]),
            "confidence": strip_markdown_cell(cells[5]),
        }
        if not any(finding.values()):
            continue
        findings.append(finding)
        if len(findings) >= 3:
            break

    if not saw_separator:
        parse_error = True
    if not findings:
        parse_error = True

    return findings, parse_error


def extract_code_tokens(text: str) -> List[str]:
    tokens = set()
    for token in re.findall(r"`([^`]+)`", text or ""):
        cleaned = token.strip()
        if cleaned:
            tokens.add(cleaned)

    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_./:-]{3,}", text or ""):
        cleaned = token.strip("`'\".,:;()[]{}")
        if not cleaned or cleaned.lower() in CODE_TOKEN_STOPWORDS:
            continue
        tokens.add(cleaned)

    return sorted(tokens)


def titles_match(left: Dict[str, str], right: Dict[str, str]) -> bool:
    left_title = normalize_match_text(left.get("bug_title", ""))
    right_title = normalize_match_text(right.get("bug_title", ""))
    if not left_title or not right_title:
        return False
    if left_title == right_title:
        return True

    title_ratio = SequenceMatcher(None, left_title, right_title).ratio()
    if title_ratio >= 0.8:
        return True

    left_tokens = set(left_title.split())
    right_tokens = set(right_title.split())
    overlap = len(left_tokens & right_tokens)
    if not overlap:
        return False

    same_file = normalize_repo_path(left.get("file", "")) == normalize_repo_path(right.get("file", ""))
    return same_file and overlap >= max(1, min(len(left_tokens), len(right_tokens)) // 2)


def reflection_confirms_finding(
    finding: Dict[str, str],
    draft_findings: List[Dict[str, str]],
) -> bool:
    for draft_finding in draft_findings:
        if titles_match(finding, draft_finding):
            return True
    return False


def has_concrete_impact(impact: str) -> bool:
    impact_text = normalize_match_text(impact)
    return any(term in impact_text for term in USER_VISIBLE_IMPACT_TERMS)


def is_speculative_finding(finding: Dict[str, str]) -> bool:
    combined = normalize_match_text(
        " ".join(
            [
                finding.get("bug_title", ""),
                finding.get("evidence", ""),
                finding.get("impact", ""),
            ]
        )
    )
    return any(term in combined.split() for term in SPECULATIVE_TERMS)


def compute_confidence_label(
    finding: Dict[str, str],
    *,
    changed_files: List[str],
    diff_text: str,
    draft_findings: List[Dict[str, str]],
) -> str:
    file_path = normalize_repo_path(finding.get("file", ""))
    evidence = finding.get("evidence", "")
    impact = finding.get("impact", "")
    diff_lower = (diff_text or "").lower()
    changed_file_set = {normalize_repo_path(path).lower() for path in changed_files}

    file_supported = bool(file_path) and (file_path.lower() in changed_file_set or file_path.lower() in diff_lower)
    evidence_tokens = extract_code_tokens(evidence)
    matched_tokens = [token for token in evidence_tokens if token.lower() in diff_lower]
    code_reference = bool(re.search(r"`[^`]+`", evidence)) or bool(re.search(r"[A-Za-z_][A-Za-z0-9_]*\(", evidence))
    exact_changed_reference = file_supported and (code_reference or bool(matched_tokens))
    direct_diff_support = exact_changed_reference or (file_supported and len(normalize_whitespace(evidence)) >= 24)
    reflection_confirmed = reflection_confirms_finding(finding, draft_findings)
    concrete_impact = has_concrete_impact(impact)
    speculative = is_speculative_finding(finding)

    score = 0
    if file_supported:
        score += 1
    if exact_changed_reference:
        score += 2
    if direct_diff_support:
        score += 1
    if reflection_confirmed:
        score += 2
    if concrete_impact:
        score += 1
    if speculative:
        score -= 2
    if len(normalize_whitespace(evidence)) < 16:
        score -= 1

    if exact_changed_reference and reflection_confirmed and not speculative:
        return "High"
    if score >= 3 and (direct_diff_support or reflection_confirmed) and not speculative:
        return "Medium"
    return "Low"


def build_structured_review_result(
    *,
    draft_review: str,
    final_review: str,
    changed_files: List[str],
    diff_text: str,
    reflection_enabled: bool,
) -> Dict[str, Any]:
    draft_findings, draft_parse_error = parse_markdown_findings(draft_review)
    final_findings, final_parse_error = parse_markdown_findings(final_review)
    final_text = str(final_review or "").strip()

    selected_findings = final_findings
    confirmation_source: List[Dict[str, str]] = draft_findings if reflection_enabled and final_findings else []
    parse_warning: Optional[str] = None
    no_strong_bug = is_no_strong_bug_response(final_text)

    if no_strong_bug:
        selected_findings = []
    elif not final_findings and reflection_enabled and draft_findings:
        selected_findings = draft_findings
        confirmation_source = []
        parse_warning = "Reflection output was malformed. Showing first-pass findings."
    elif not final_findings and final_parse_error:
        parse_warning = "The model output could not be parsed into findings rows."
    elif final_findings and final_parse_error:
        parse_warning = "Some malformed findings rows were ignored."
    elif not reflection_enabled and draft_findings and draft_parse_error:
        parse_warning = "Some malformed findings rows were ignored."

    findings: List[Dict[str, str]] = []
    for finding in selected_findings[:3]:
        confidence = compute_confidence_label(
            finding,
            changed_files=changed_files,
            diff_text=diff_text,
            draft_findings=confirmation_source,
        )
        findings.append(
            {
                "bug_title": finding.get("bug_title", ""),
                "file": finding.get("file", "") or "-",
                "evidence": finding.get("evidence", ""),
                "risk_type": finding.get("risk_type", "") or "Other",
                "impact": finding.get("impact", ""),
                "confidence": confidence,
                "confidence_class": f"confidence-{confidence.lower()}",
            }
        )

    if not findings and not no_strong_bug and not parse_warning and (draft_parse_error or final_parse_error):
        parse_warning = "The model output could not be parsed into findings rows."

    return {
        "findings": findings,
        "no_strong_bug": no_strong_bug and not findings,
        "findings_parse_warning": parse_warning,
    }


def save_json_debug_file(filename: str, payload: Any) -> Path:
    DEBUG_DIR.mkdir(exist_ok=True)
    file_path = DEBUG_DIR / filename
    file_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    logger.info("Saved debug JSON: %s", file_path)
    return file_path


def save_debug_artifacts(
    compressed_diff_text: str,
    draft_review: str,
    final_review: str,
    logger,
) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    compressed_path = DEBUG_DIR / f"compressed_diff_{ts}.txt"
    draft_path = DEBUG_DIR / f"draft_review_{ts}.txt"
    final_path = DEBUG_DIR / f"final_review_{ts}.txt"

    compressed_path.write_text(compressed_diff_text, encoding="utf-8")
    draft_path.write_text(draft_review, encoding="utf-8")
    final_path.write_text(final_review, encoding="utf-8")

    logger.info("Saved compressed diff: %s", compressed_path)
    logger.info("Saved draft review: %s", draft_path)
    logger.info("Saved final review: %s", final_path)
    logger.info("Draft review length: %s chars", len(draft_review))
    logger.info("Final review length: %s chars", len(final_review))


def load_rules_text() -> str:
    try:
        rules = RULES_PATH.read_text(encoding="utf-8")
        logger.info("Rules loaded from rules.txt")
        return rules
    except Exception as exc:
        logger.error("Error reading rules.txt: %s", exc)
        return ""


def build_changed_files_summary(changed_files: List[str]) -> str:
    if not changed_files:
        return "None"

    summary = ", ".join(changed_files[:20])
    if len(changed_files) > 20:
        summary += f", ... and {len(changed_files) - 20} more files"
    return summary


def extract_review_version(
    pull_request: dict,
    *,
    commit_key: str,
    ref_key: str,
) -> Tuple[str, str]:
    commit_ref = pull_request.get(commit_key) or {}
    commit_id = (commit_ref.get("commitId") or "").strip()
    if commit_id:
        return commit_id, "commit"

    ref_name = (pull_request.get(ref_key) or "").strip()
    if ref_name:
        return ref_name, "branch"

    raise ValueError(f"Missing {commit_key} and {ref_key} in Azure DevOps pull request payload.")


def missing_ado_config() -> List[str]:
    required_settings = {
        "ADO_ORG": ADO_ORG,
        "ADO_PROJECT": ADO_PROJECT,
        "ADO_REPO": ADO_REPO,
        "ADO_PAT": ADO_PAT,
    }
    return [name for name, value in required_settings.items() if not value]


def normalize_pull_request_resource(resource: Dict[str, Any]) -> Dict[str, Any]:
    pull_request = resource.get("pullRequest")
    if not isinstance(pull_request, dict):
        return resource

    normalized = dict(pull_request)
    for key in (
        "pullRequestId",
        "repository",
        "sourceRefName",
        "targetRefName",
        "title",
        "description",
        "lastMergeSourceCommit",
        "lastMergeTargetCommit",
    ):
        if key not in normalized and key in resource:
            normalized[key] = resource.get(key)
    return normalized


def parse_pull_request_id(raw_value: Any) -> Optional[int]:
    if raw_value is None:
        return None

    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def log_pull_request_context(event_type: str, pull_request: Dict[str, Any]) -> None:
    repository = pull_request.get("repository") or {}
    logger.info(
        "Azure DevOps PR event=%s pullRequestId=%s repository.id=%s repository.name=%s sourceRefName=%s targetRefName=%s",
        event_type or "unknown",
        pull_request.get("pullRequestId"),
        repository.get("id"),
        repository.get("name"),
        pull_request.get("sourceRefName"),
        pull_request.get("targetRefName"),
    )


def fetch_pull_request_debug_payloads(pull_request_id: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    pr_details: Optional[Dict[str, Any]]
    pr_changes_payload: Optional[Dict[str, Any]]

    try:
        pr_details = azure_devops.fetch_pull_request_details(
            requests,
            ADO_ORG or "",
            ADO_PROJECT or "",
            ADO_REPO or "",
            pull_request_id,
            ADO_PAT or "",
        )
        save_json_debug_file("pr_details.json", pr_details)
    except Exception as exc:
        logger.exception("Failed to fetch Azure DevOps PR details for %s", pull_request_id)
        pr_details = None
        save_json_debug_file("pr_details.json", {"error": str(exc), "pullRequestId": pull_request_id})

    try:
        # The existing service hook should already identify the PR; iteration 1 keeps the intake predictable.
        pr_changes_payload = azure_devops.fetch_pull_request_iteration_changes(
            requests,
            ADO_ORG or "",
            ADO_PROJECT or "",
            ADO_REPO or "",
            pull_request_id,
            1,
            ADO_PAT or "",
        )
    except Exception as exc:
        logger.exception("Failed to fetch Azure DevOps PR changes for %s", pull_request_id)
        pr_changes_payload = None
        save_json_debug_file("pr_changes.json", {"error": str(exc), "pullRequestId": pull_request_id})

    return pr_details, pr_changes_payload


def save_last_result(
    *,
    pull_request_id: int,
    repository_name: str,
    source_branch: str,
    target_branch: str,
    event_type: str,
    changed_files: List[str],
    diff_text: str,
    review_text: str,
    findings: List[Dict[str, str]],
    no_strong_bug: bool,
    findings_parse_warning: Optional[str],
) -> None:
    global LAST_RESULT
    LAST_RESULT = {
        "pr_id": pull_request_id,
        "repository": repository_name,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "event_type": event_type,
        "changed_files": changed_files,
        "diff_text": diff_text,
        "review_text": review_text,
        "findings": findings,
        "no_strong_bug": no_strong_bug,
        "findings_parse_warning": findings_parse_warning,
    }


def process_azure_devops_pull_request_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    missing_config = missing_ado_config()
    if missing_config:
        logger.warning("Missing Azure DevOps configuration: %s", ", ".join(missing_config))
        return {
            "status": "accepted",
            "reason": "missing Azure DevOps configuration",
            "missing": missing_config,
        }

    resource = payload.get("resource")
    if not isinstance(resource, dict):
        logger.warning("Azure DevOps webhook payload is missing a usable resource object.")
        return {"status": "accepted", "reason": "missing resource"}

    pull_request = normalize_pull_request_resource(resource)
    event_type = str(payload.get("eventType") or "").strip()
    log_pull_request_context(event_type, pull_request)

    pull_request_id = parse_pull_request_id(pull_request.get("pullRequestId"))
    if pull_request_id is None:
        logger.warning("Azure DevOps webhook payload is missing pullRequestId.")
        return {"status": "accepted", "reason": "missing pullRequestId"}

    pr_details, pr_changes_payload = fetch_pull_request_debug_payloads(pull_request_id)
    if not pr_details or not pr_changes_payload:
        return {
            "status": "accepted",
            "eventType": event_type,
            "pullRequestId": pull_request_id,
            "reason": "failed to fetch Azure DevOps PR data",
        }

    repo_api_url = azure_devops.build_repo_api_url_from_context(
        ADO_ORG or "",
        ADO_PROJECT or "",
        ADO_REPO or "",
    )
    changes = azure_devops.extract_iteration_changes(pr_changes_payload)
    changed_files = azure_devops.extract_changed_paths(pr_changes_payload)
    changed_files_summary = build_changed_files_summary(changed_files)
    logger.info("Changed files: %s", changed_files_summary)

    diff_text = ""
    compressed_diff_text = ""
    source_branch = str(pr_details.get("sourceRefName") or pull_request.get("sourceRefName") or "")
    target_branch = str(pr_details.get("targetRefName") or pull_request.get("targetRefName") or "")
    repository_name = str(
        (pr_details.get("repository") or {}).get("name")
        or (pull_request.get("repository") or {}).get("name")
        or ADO_REPO
        or ""
    )
    try:
        source_version, source_version_type = extract_review_version(
            pr_details,
            commit_key="lastMergeSourceCommit",
            ref_key="sourceRefName",
        )
        target_version, target_version_type = extract_review_version(
            pr_details,
            commit_key="lastMergeTargetCommit",
            ref_key="targetRefName",
        )
        if source_version_type == "commit" and target_version_type == "commit":
            try:
                azure_devops.fetch_commit_diff(
                    requests,
                    ADO_ORG or "",
                    ADO_PROJECT or "",
                    ADO_REPO or "",
                    target_version,
                    source_version,
                    ADO_PAT or "",
                )
            except Exception as exc:
                logger.exception("Unable to fetch commit comparison JSON for PR %s", pull_request_id)
                save_json_debug_file(
                    "pr_commit_diff.json",
                    {"error": str(exc), "pullRequestId": pull_request_id},
                )
        else:
            save_json_debug_file(
                "pr_commit_diff.json",
                {
                    "warning": "Commit comparison skipped because commit ids were not available.",
                    "pullRequestId": pull_request_id,
                },
            )

        diff_text = azure_devops.build_unified_diff_text(
            requests,
            repo_api_url,
            ADO_PAT or "",
            changes,
            target_version,
            target_version_type,
            source_version,
            source_version_type,
        )
        compressed_diff_text = azure_devops.format_compressed_diff_for_prompt(diff_text)
    except Exception as exc:
        logger.exception("Unable to build a unified diff for PR %s", pull_request_id)
        diff_text = f"Diff generation failed: {exc}"
        azure_devops.save_debug_text("azure_unified_diff.txt", diff_text)

    rules = load_rules_text()
    draft_review = ""
    final_review = ""
    if compressed_diff_text:
        prompt = build_main_review_prompt(
            pr_title=pr_details.get("title", ""),
            pr_description=pr_details.get("description", ""),
            changed_files=changed_files_summary,
            compressed_diff=compressed_diff_text,
            ticket_context=None,
            extra_context=f"Rules:\n{rules}" if rules else None,
        )

        draft_review = call_groq_review(prompt, temperature=0.2)
        final_review = draft_review

        if ENABLE_REFLECTION:
            logger.info("Running reflection pass to validate findings...")
            reflection_prompt = build_reflection_prompt(draft_review, compressed_diff_text)
            final_review = call_groq_review(reflection_prompt, temperature=0.1)
            logger.info("Reflection pass completed")
    else:
        final_review = NO_STRONG_BUG_TEXT
        draft_review = final_review

    if not final_review or final_review.lower() == "none":
        final_review = NO_STRONG_BUG_TEXT

    structured_review = build_structured_review_result(
        draft_review=draft_review,
        final_review=final_review,
        changed_files=changed_files,
        diff_text=diff_text or compressed_diff_text,
        reflection_enabled=ENABLE_REFLECTION,
    )

    save_debug_artifacts(
        compressed_diff_text=compressed_diff_text or diff_text or "No reviewable text diff was found.",
        draft_review=draft_review,
        final_review=final_review,
        logger=logger,
    )
    save_last_result(
        pull_request_id=pull_request_id,
        repository_name=repository_name,
        source_branch=source_branch,
        target_branch=target_branch,
        event_type=event_type,
        changed_files=changed_files,
        diff_text=diff_text,
        review_text=final_review,
        findings=structured_review["findings"],
        no_strong_bug=structured_review["no_strong_bug"],
        findings_parse_warning=structured_review["findings_parse_warning"],
    )

    # Local debug and AI review generation are enabled first; comment posting stays disabled for now.
    logger.info("Azure DevOps PR %s processed locally. Comment posting is disabled.", pull_request_id)
    return {
        "status": "processed",
        "eventType": event_type,
        "pullRequestId": pull_request_id,
        "reviewGenerated": bool(compressed_diff_text),
        "changedFiles": changed_files,
        "preview_url": "/review-result",
    }


# ----- WEBHOOK -----
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(silent=True)
        headers_payload = {key: value for key, value in request.headers.items()}

        # Azure service hook requests are captured raw so you can inspect the exact payload shape locally.
        save_json_debug_file("azure_headers.json", headers_payload)
        save_json_debug_file("azure_payload.json", payload if payload is not None else {})

        if not isinstance(payload, dict):
            logger.warning("Webhook payload was not a JSON object.")
            return jsonify({"status": "accepted", "reason": "payload was not a JSON object"}), 200

        event_type = str(payload.get("eventType") or "").strip()
        logger.info("Received Azure DevOps event: %s", event_type or "unknown")

        if event_type not in SUPPORTED_AZURE_EVENTS:
            logger.info("Ignoring unsupported Azure DevOps event type: %s", event_type or "unknown")
            return jsonify({"status": "ignored", "eventType": event_type or "unknown"}), 200

        return jsonify(process_azure_devops_pull_request_event(payload)), 200
    except Exception as exc:
        logger.exception("Webhook error occurred")
        return jsonify({"status": "accepted", "reason": "processing error"}), 200


@app.route("/review-result", methods=["GET"])
def review_result():
    return render_template("review_result.html", data=LAST_RESULT or None)


# ----- FOR VERCEL -----
app = app
