from datetime import datetime
import json
import logging
import os
from pathlib import Path
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
            reflection_prompt = build_reflection_prompt(draft_review)
            final_review = call_groq_review(reflection_prompt, temperature=0.1)
            logger.info("Reflection pass completed")
    else:
        final_review = "No reviewable text diff was found in this pull request."
        draft_review = final_review

    if not final_review or final_review.lower() == "none":
        final_review = "No material bug-risk findings detected."

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
