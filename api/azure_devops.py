from importlib.util import module_from_spec, spec_from_file_location
import json
import difflib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set
from urllib.parse import quote, urlparse

from requests.auth import HTTPBasicAuth

try:
    from compression.compress_pr_diff import SKIP_EXT, compress_pr_diff
except ModuleNotFoundError:
    compress_pr_diff_path = Path(__file__).resolve().parent.parent / "compression" / "compress_pr_diff.py"
    spec = spec_from_file_location("ai_code_review_compress_pr_diff", compress_pr_diff_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load diff compressor from {compress_pr_diff_path}")

    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    SKIP_EXT = module.SKIP_EXT
    compress_pr_diff = module.compress_pr_diff

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEBUG_DIR = PROJECT_ROOT / "debug"

API_VERSION = "7.1"
REQUEST_TIMEOUT_SECONDS = 30
DEBUG_DIR.mkdir(exist_ok=True)


def normalize_repo_relative_path(path: str) -> str:
    return (path or "").lstrip("/")


def dedupe_keep_order(items: Iterable[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def build_basic_auth(token: str) -> HTTPBasicAuth:
    return HTTPBasicAuth("", token)


def save_debug_json(filename: str, payload: Any) -> Path:
    path = DEBUG_DIR / filename
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return path


def save_debug_text(filename: str, content: str) -> Path:
    path = DEBUG_DIR / filename
    path.write_text(content, encoding="utf-8")
    return path


def build_repo_api_url(
    organization_url: str,
    project_name: str,
    repository_id: str,
) -> str:
    base = organization_url.rstrip("/")
    project_segment = f"/{quote(project_name, safe='')}" if project_name else ""
    repository_segment = quote(repository_id, safe="")
    return f"{base}{project_segment}/_apis/git/repositories/{repository_segment}"


def build_repo_api_url_from_context(
    organization: str,
    project_name: str,
    repository_id: str,
) -> str:
    organization_segment = quote((organization or "").strip(), safe="")
    organization_url = f"https://dev.azure.com/{organization_segment}"
    return build_repo_api_url(organization_url, project_name, repository_id)


def extract_repo_api_url(payload: Dict[str, Any], pull_request: Dict[str, Any]) -> str:
    repository = pull_request.get("repository") or {}
    repo_api_url = (repository.get("url") or "").strip()
    if repo_api_url:
        return repo_api_url.rstrip("/")

    resource_containers = payload.get("resourceContainers") or {}
    account = resource_containers.get("account") or {}
    organization_url = (account.get("baseUrl") or "").strip().rstrip("/")

    if not organization_url:
        repo_remote_url = (repository.get("remoteUrl") or "").strip()
        if repo_remote_url:
            parsed = urlparse(repo_remote_url)
            segments = [segment for segment in parsed.path.split("/") if segment]
            if segments:
                organization_url = f"{parsed.scheme}://{parsed.netloc}/{segments[0]}"

    project = repository.get("project") or {}
    project_name = (project.get("name") or "").strip()
    repository_id = (repository.get("id") or "").strip()

    if not organization_url or not repository_id:
        raise ValueError("Unable to determine the Azure DevOps repository API URL from the webhook payload.")

    return build_repo_api_url(organization_url, project_name, repository_id)


def fetch_pull_request_details(
    http: Any,
    organization: str,
    project_name: str,
    repository_id: str,
    pull_request_id: int,
    token: str,
) -> Dict[str, Any]:
    repo_api_url = build_repo_api_url_from_context(organization, project_name, repository_id)
    payload = fetch_json(
        http,
        f"{repo_api_url}/pullrequests/{pull_request_id}",
        token,
        params={"api-version": API_VERSION},
    )
    return payload or {}


def fetch_pull_request_iteration_changes(
    http: Any,
    organization: str,
    project_name: str,
    repository_id: str,
    pull_request_id: int,
    iteration_id: int,
    token: str,
) -> Dict[str, Any]:
    repo_api_url = build_repo_api_url_from_context(organization, project_name, repository_id)
    payload = fetch_json(
        http,
        f"{repo_api_url}/pullRequests/{pull_request_id}/iterations/{iteration_id}/changes",
        token,
        params={"api-version": API_VERSION},
    )
    result = payload or {}
    save_debug_json("pr_changes.json", result)
    return result


def fetch_commit_diff(
    http: Any,
    organization: str,
    project_name: str,
    repository_id: str,
    base_commit: str,
    target_commit: str,
    token: str,
) -> Dict[str, Any]:
    repo_api_url = build_repo_api_url_from_context(organization, project_name, repository_id)
    payload = fetch_json(
        http,
        f"{repo_api_url}/diffs/commits",
        token,
        params={
            "baseVersion": base_commit,
            "baseVersionType": "commit",
            "targetVersion": target_commit,
            "targetVersionType": "commit",
            "api-version": API_VERSION,
        },
    )
    result = payload or {}
    save_debug_json("pr_commit_diff.json", result)
    return result


def extract_iteration_changes(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    change_entries = payload.get("changeEntries")
    if isinstance(change_entries, list):
        return change_entries

    changes = payload.get("changes")
    if isinstance(changes, list):
        return changes

    return []


def extract_changed_paths(payload: Dict[str, Any]) -> List[str]:
    return extract_changed_files(extract_iteration_changes(payload))


def parse_change_types(change_type: str) -> Set[str]:
    parsed: Set[str] = set()
    for chunk in (change_type or "").replace(";", ",").split(","):
        value = chunk.strip().lower()
        if not value:
            continue
        parsed.add(value)
        for part in value.split():
            parsed.add(part)
    return parsed


def fetch_json(
    http: Any,
    url: str,
    token: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    allow_not_found: bool = False,
) -> Optional[Dict[str, Any]]:
    response = http.get(
        url,
        auth=build_basic_auth(token),
        headers={"Accept": "application/json"},
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if allow_not_found and response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def fetch_pull_request_changes(
    http: Any,
    repo_api_url: str,
    token: str,
    base_version: str,
    base_version_type: str,
    target_version: str,
    target_version_type: str,
) -> List[Dict[str, Any]]:
    changes: List[Dict[str, Any]] = []
    skip = 0
    top = 200

    while True:
        payload = fetch_json(
            http,
            f"{repo_api_url}/diffs/commits",
            token,
            params={
                "baseVersion": base_version,
                "baseVersionType": base_version_type,
                "targetVersion": target_version,
                "targetVersionType": target_version_type,
                "$top": top,
                "$skip": skip,
                "api-version": API_VERSION,
            },
        ) or {}

        batch = payload.get("changes", [])
        changes.extend(batch)

        if payload.get("allChangesIncluded", True) or not batch:
            return changes

        skip += len(batch)


def is_probably_binary_path(path: str) -> bool:
    return Path(path or "").suffix.lower() in SKIP_EXT


def fetch_item_text(
    http: Any,
    repo_api_url: str,
    token: str,
    path: str,
    version: str,
    version_type: str,
) -> Optional[str]:
    if not path or not version or is_probably_binary_path(path):
        return None

    payload = fetch_json(
        http,
        f"{repo_api_url}/items",
        token,
        params={
            "path": path,
            "includeContent": "true",
            "includeContentMetadata": "true",
            "versionDescriptor.version": version,
            "versionDescriptor.versionType": version_type,
            "api-version": API_VERSION,
        },
        allow_not_found=True,
    )

    if not payload:
        return None

    content_metadata = payload.get("contentMetadata") or {}
    if content_metadata.get("isBinary") or content_metadata.get("isImage"):
        return None

    content = payload.get("content")
    if isinstance(content, str):
        return content
    return None


def fetch_file_content_at_commit(
    http: Any,
    repo_api_url: str,
    token: str,
    path: str,
    commit_id: str,
) -> str:
    text = fetch_item_text(
        http,
        repo_api_url,
        token,
        path,
        commit_id,
        "commit",
    )
    return text or ""


def build_unified_diff(
    old_text: str,
    new_text: str,
    path: str,
    *,
    change_type: str = "edit",
    original_path: Optional[str] = None,
) -> str:
    current_path = path or ""
    previous_path = original_path or current_path
    change_types = parse_change_types(change_type)
    is_add = "add" in change_types
    is_delete = "delete" in change_types
    is_rename = "rename" in change_types and normalize_repo_relative_path(previous_path) != normalize_repo_relative_path(current_path)

    old_rel_path = normalize_repo_relative_path(previous_path)
    new_rel_path = normalize_repo_relative_path(current_path)
    fromfile = "/dev/null" if is_add else f"a/{old_rel_path}"
    tofile = "/dev/null" if is_delete else f"b/{new_rel_path}"

    diff_lines = list(
        difflib.unified_diff(
            (old_text or "").splitlines(),
            (new_text or "").splitlines(),
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
        )
    )

    if not diff_lines and not is_rename:
        return ""

    lines = [f"diff --git {fromfile} {tofile}"]
    if is_rename:
        lines.append(f"rename from {old_rel_path}")
        lines.append(f"rename to {new_rel_path}")
    lines.extend(diff_lines)
    return "\n".join(lines)


def build_unified_diff_for_change(
    http: Any,
    repo_api_url: str,
    token: str,
    change: Dict[str, Any],
    base_version: str,
    base_version_type: str,
    target_version: str,
    target_version_type: str,
) -> Optional[str]:
    item = change.get("item") or {}
    if item.get("isFolder"):
        return None

    current_path = item.get("path") or ""
    original_path = (
        change.get("originalPath")
        or change.get("sourceServerItem")
        or item.get("originalPath")
        or current_path
    )
    change_types = parse_change_types(str(change.get("changeType") or ""))

    if is_probably_binary_path(current_path or original_path):
        return None

    is_add = "add" in change_types
    is_delete = "delete" in change_types

    before_text = "" if is_add else fetch_item_text(
        http,
        repo_api_url,
        token,
        original_path,
        base_version,
        base_version_type,
    )
    after_text = "" if is_delete else fetch_item_text(
        http,
        repo_api_url,
        token,
        current_path,
        target_version,
        target_version_type,
    )

    if before_text is None and after_text is None:
        return None

    diff_text = build_unified_diff(
        before_text or "",
        after_text or "",
        current_path,
        change_type=str(change.get("changeType") or ""),
        original_path=original_path,
    )
    if not diff_text:
        return None

    return diff_text


def build_unified_diff_text(
    http: Any,
    repo_api_url: str,
    token: str,
    changes: Sequence[Dict[str, Any]],
    base_version: str,
    base_version_type: str,
    target_version: str,
    target_version_type: str,
) -> str:
    diff_chunks: List[str] = []
    for change in changes:
        diff_chunk = build_unified_diff_for_change(
            http,
            repo_api_url,
            token,
            change,
            base_version,
            base_version_type,
            target_version,
            target_version_type,
        )
        if diff_chunk:
            diff_chunks.append(diff_chunk)
    diff_text = "\n\n".join(diff_chunks)
    save_debug_text("azure_unified_diff.txt", diff_text)
    return diff_text


def extract_changed_files(changes: Sequence[Dict[str, Any]]) -> List[str]:
    changed_files = []
    for change in changes:
        item = change.get("item") or {}
        if item.get("isFolder"):
            continue
        path = normalize_repo_relative_path(item.get("path") or "")
        if path:
            changed_files.append(path)
    return dedupe_keep_order(changed_files)


def format_compressed_diff_for_prompt(diff_text: str) -> str:
    if not diff_text.strip():
        return ""

    payload = compress_pr_diff(diff_text)
    sections: List[str] = []

    for item in payload.get("included_patches", []):
        patch_text = (item.get("patch") or "").strip()
        if patch_text:
            sections.append(patch_text)

    other_modified_files = payload.get("other_modified_files") or []
    if other_modified_files:
        sections.append(
            "Other modified files:\n" + "\n".join(f"- {path}" for path in other_modified_files)
        )

    deleted_files = payload.get("deleted_files") or []
    if deleted_files:
        sections.append(
            "Deleted files:\n" + "\n".join(f"- {path}" for path in deleted_files)
        )

    return "\n\n".join(sections).strip()


def post_pull_request_comment(
    http: Any,
    repo_api_url: str,
    token: str,
    pull_request_id: int,
    content: str,
) -> Dict[str, Any]:
    response = http.post(
        f"{repo_api_url}/pullRequests/{pull_request_id}/threads",
        auth=build_basic_auth(token),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        params={"api-version": API_VERSION},
        json={
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": content,
                    "commentType": 1,
                }
            ],
            "status": 1,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()
