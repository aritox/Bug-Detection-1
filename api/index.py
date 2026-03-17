from flask import Flask, request, jsonify
import os
import logging
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from groq import Groq

try:
    from .prompt_builder import build_main_review_prompt, build_reflection_prompt
except ImportError:
    from prompt_builder import build_main_review_prompt, build_reflection_prompt

# Load .env before reading environment variables.
load_dotenv()

# ----- APP SETUP -----
app = Flask(__name__)
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
ENABLE_REFLECTION = os.getenv("ENABLE_REFLECTION", "true").lower() == "true"
RULES_PATH = Path(__file__).resolve().parent.parent / "rules.txt"

# ----- LOGGING CONFIG -----
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

logger.info("GROQ_API_KEY exists: %s", bool(GROQ_API_KEY))
if not GROQ_API_KEY:
    error_message = "Missing GROQ_API_KEY or API_KEY environment variable. Set one in your .env file."
    logger.error(error_message)
    raise RuntimeError(error_message)

client = Groq(api_key=GROQ_API_KEY)
logger.info("Groq client initialized successfully")
logger.info("Reflection pass enabled: %s", ENABLE_REFLECTION)

# ----- HEALTH CHECK -----
@app.route("/healthz", methods=["GET"])
def health_check():
    logger.info("Health check triggered")
    return jsonify({"status": "ok"}), 200


def get_last_position(patch):
    """Calculate the last position in the patch for GitHub review comments."""
    return sum(1 for line in patch.splitlines() if line.startswith("+"))


def extract_issues_per_file(result):
    """
    Parses LLM result and returns dict of filename => issues block.
    Assumes result contains sections like:
    Filename: api/index.py
    1. Issue:
       Location:
       Solution:
    """
    file_issues = {}
    current_file = None
    lines = result.splitlines()
    buffer = []
    for line in lines:
        if line.strip().startswith("Filename:"):
            if current_file and buffer:
                file_issues[current_file] = "\n".join(buffer).strip()
                buffer = []
            current_file = line.strip().split("Filename:")[-1].strip()
        elif current_file:
            buffer.append(line)
    if current_file and buffer:
        file_issues[current_file] = "\n".join(buffer).strip()
    return file_issues


def call_groq_review(prompt: str, temperature: float = 0.2) -> str:
    """
    Call Groq API with the given prompt.
    
    Args:
        prompt: The full prompt to send to the model
        temperature: Model temperature (lower = more deterministic)
    
    Returns:
        The model's response content as string
    """
    logger.info("Calling Groq API with model: %s", GROQ_MODEL)
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature
    )
    result = response.choices[0].message.content.strip()
    logger.info("Groq response received (%d chars)", len(result))
    return result


def save_debug_artifacts(
    compressed_diff_text: str,
    draft_review: str,
    final_review: str,
    logger,
) -> None:
    debug_dir = Path("debug")
    debug_dir.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    compressed_path = debug_dir / f"compressed_diff_{ts}.txt"
    draft_path = debug_dir / f"draft_review_{ts}.txt"
    final_path = debug_dir / f"final_review_{ts}.txt"

    compressed_path.write_text(compressed_diff_text, encoding="utf-8")
    draft_path.write_text(draft_review, encoding="utf-8")
    final_path.write_text(final_review, encoding="utf-8")

    logger.info("Saved compressed diff: %s", compressed_path)
    logger.info("Saved draft review: %s", draft_path)
    logger.info("Saved final review: %s", final_path)
    logger.info("Draft review length: %s chars", len(draft_review))
    logger.info("Final review length: %s chars", len(final_review))


# ----- GITHUB WEBHOOK -----
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        event = request.headers.get("X-GitHub-Event")
        payload = request.get_json()

        logger.info(f"Received GitHub event: {event}")

        if event == "issue_comment":
            comment_body = payload["comment"]["body"]
            logger.info(f"Comment content: {comment_body}")

            if comment_body.strip() == "/ai-bot":
                if not GITHUB_TOKEN:
                    logger.error("Missing GITHUB_TOKEN environment variable")
                    return jsonify({"error": "Missing GITHUB_TOKEN"}), 500

                if not client:
                    logger.error("Missing GROQ_API_KEY environment variable")
                    return jsonify({"error": "Missing GROQ_API_KEY"}), 500

                pr_url = payload["issue"]["pull_request"]["url"]
                pr_number = pr_url.split("/")[-1]
                owner = payload["repository"]["owner"]["login"]
                repo = payload["repository"]["name"]

                logger.info(f"Triggered on PR: {pr_url}")
                logger.info("Fetching PR diff...")

                # Fetch the diff
                diff_response = requests.get(
                    pr_url + ".diff",
                    headers={
                        "Authorization": f"Bearer {GITHUB_TOKEN}",
                        "Accept": "application/vnd.github.v3.diff"
                    }
                )
                diff = diff_response.text
                logger.info("Diff received (%d chars), first 10 lines:", len(diff))
                logger.info("\n".join(diff.splitlines()[:10]))

                # Load rules file
                try:
                    with RULES_PATH.open(encoding="utf-8") as f:
                        rules = f.read()
                        logger.info("Rules loaded from rules.txt")
                except Exception as e:
                    logger.error(f"Error reading rules.txt: {e}")
                    rules = ""

                # Extract PR metadata
                pr_data = requests.get(pr_url, headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}"
                }).json()
                commit_id = pr_data["head"]["sha"]
                pr_title = pr_data.get("title", "")
                pr_description = pr_data.get("body", "")

                logger.info(f"PR title: {pr_title}")
                logger.info(f"Commit ID: {commit_id}")

                # Get changed files list
                files_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
                files_data = requests.get(files_url, headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}"
                }).json()

                changed_files_list = [f["filename"] for f in files_data]
                changed_files_str = ", ".join(changed_files_list[:20])
                if len(changed_files_list) > 20:
                    changed_files_str += f", ... and {len(changed_files_list) - 20} more files"

                logger.info(f"Changed files: {changed_files_str}")

                # Build the main review prompt using the new prompt_builder module
                prompt = build_main_review_prompt(
                    pr_title=pr_title,
                    pr_description=pr_description,
                    changed_files=changed_files_str,
                    compressed_diff=diff,
                    ticket_context=None,
                    extra_context=f"Rules:\n{rules}" if rules else None
                )

                # Get the initial review
                draft_review = call_groq_review(prompt, temperature=0.2)
                result = draft_review

                # Optional: Run reflection pass to filter hallucinations
                if ENABLE_REFLECTION:
                    logger.info("Running reflection pass to validate findings...")
                    reflection_prompt = build_reflection_prompt(result)
                    result = call_groq_review(reflection_prompt, temperature=0.1)
                    logger.info("Reflection pass completed")

                final_review = result
                save_debug_artifacts(
                    compressed_diff_text=diff,
                    draft_review=draft_review,
                    final_review=final_review,
                    logger=logger,
                )

                if not result or result.lower() == "none":
                    logger.info("No violations found. Skipping comment.")
                    return jsonify({"status": "no violations"})

                # Parse AI result by file (legacy extraction for GitHub comments)
                file_issues = extract_issues_per_file(result)

                headers = {
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json"
                }

                review_comment_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/comments"

                # Post comments for each file with issues
                comment_count = 0
                for file in files_data:
                    filename = file["filename"]
                    patch = file.get("patch", "")
                    if filename in file_issues:
                        position = get_last_position(patch)
                        body = f"🤖 AI Code Review Feedback for `{filename}`:\n\n{file_issues[filename]}"

                        comment_payload = {
                            "body": body,
                            "commit_id": commit_id,
                            "path": filename,
                            "position": position
                        }

                        r = requests.post(review_comment_url, json=comment_payload, headers=headers)
                        if r.status_code in [200, 201]:
                            comment_count += 1
                            logger.info(f"Posted comment for {filename}: {r.status_code}")
                        else:
                            logger.warning(f"Failed to post comment for {filename}: {r.status_code}")

                logger.info(f"Review complete. Posted {comment_count} file-specific comments.")

        return jsonify({"status": "ok"})
    except Exception as e:
        logger.exception("Webhook error occurred")
        return jsonify({"error": str(e)}), 500


# ----- FOR VERCEL -----
app = app
