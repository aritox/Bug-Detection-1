import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_MAX_TOKENS = 12000
DEFAULT_BUFFER_TOKENS = 800
DEFAULT_OTHER_FILES_BUDGET = 800
DEFAULT_DELETED_FILES_BUDGET = 400

# Skip binary/non-code artifacts by extension.
SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".pdf", ".zip", ".rar", ".7z", ".gz", ".tgz", ".bz2",
    ".exe", ".dll", ".so", ".dylib", ".jar", ".war", ".class",
    ".mp4", ".mov", ".avi", ".mkv", ".mp3", ".wav",
    ".pdb", ".bin", ".dat", ".woff", ".woff2", ".ttf", ".eot",
}

# Optional generated/noisy files.
SKIP_FILENAMES_EXACT = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "cargo.lock",
}

SKIP_PATH_CONTAINS = {
    "/dist/",
    "/build/",
    "/node_modules/",
    "/.venv/",
    "/venv/",
}

LANG_PRIORITY = [
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".go", ".rb",
    ".php", ".cpp", ".c", ".h", ".sql", ".yaml", ".yml", ".json", ".md", ".txt",
]


def _init_tiktoken_encoder() -> Optional[Any]:
    try:
        import tiktoken  # type: ignore
    except Exception:
        return None

    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


_TIKTOKEN_ENCODER = _init_tiktoken_encoder()
TOKEN_ESTIMATOR = "tiktoken" if _TIKTOKEN_ENCODER else "char_heuristic"


def estimate_tokens(text: str) -> int:
    """Estimate token count using tiktoken when available, otherwise a char heuristic."""
    if not text:
        return 0

    if _TIKTOKEN_ENCODER is not None:
        try:
            return len(_TIKTOKEN_ENCODER.encode(text))
        except Exception:
            pass

    # Conservative fallback: ~1 token per 4 chars.
    return max(1, math.ceil(len(text) / 4))


def _normalize_path(path: str) -> str:
    path = (path or "").strip()
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _dedupe_keep_order(items: Sequence[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if not item:
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


@dataclass
class Hunk:
    header: str
    lines: List[str]

    def has_add(self) -> bool:
        for ln in self.lines:
            if ln.startswith("+"):
                return True
        return False

    def has_del(self) -> bool:
        for ln in self.lines:
            if ln.startswith("-"):
                return True
        return False

    def is_deletion_only(self) -> bool:
        return self.has_del() and not self.has_add()

    def text(self) -> str:
        return self.header + "\n" + "\n".join(self.lines) + "\n"


@dataclass
class FilePatch:
    old_path: str = ""
    new_path: str = ""
    hunks: List[Hunk] = field(default_factory=list)
    is_deleted_file: bool = False
    is_binary: bool = False
    is_rename: bool = False

    def filename(self) -> str:
        # Prefer new path unless deleted.
        candidate = self.new_path if self.new_path and self.new_path != "/dev/null" else self.old_path
        return _normalize_path(candidate)

    def ext(self) -> str:
        return Path(self.filename()).suffix.lower()

    def should_skip(self, skip_generated: bool = True) -> bool:
        if self.is_binary:
            return True

        file_path = self.filename().lower()
        file_name = Path(file_path).name

        if self.ext() in SKIP_EXT:
            return True

        if skip_generated:
            if file_name in SKIP_FILENAMES_EXACT:
                return True
            normalized = "/" + file_path.replace("\\", "/").strip("/") + "/"
            for seg in SKIP_PATH_CONTAINS:
                if seg in normalized:
                    return True

        return False

    def filtered_hunks_keep_additions(self) -> List[Hunk]:
        # Remove deletion-only hunks. Keep additions and edits.
        return [h for h in self.hunks if not h.is_deletion_only()]

    def patch_text(self, filtered: bool = True) -> str:
        hunks = self.filtered_hunks_keep_additions() if filtered else self.hunks
        if not hunks:
            return ""

        lines = [f"File: {self.filename()}"]
        if self.is_rename:
            old_name = _normalize_path(self.old_path)
            new_name = _normalize_path(self.new_path)
            lines.append(f"Rename: {old_name} -> {new_name}")

        body = "".join(h.text() for h in hunks)
        return "\n".join(lines) + "\n" + body


def language_rank(ext: str) -> int:
    ext = (ext or "").lower()
    try:
        return LANG_PRIORITY.index(ext)
    except ValueError:
        return len(LANG_PRIORITY) + 100


def parse_unified_diff(diff_text: str) -> List[FilePatch]:
    """
    Parse unified diff into file patches.
    Robust to:
      - rename metadata
      - multiple hunks per file
      - sections that start with ---/+++ without a preceding diff --git header
    """
    lines = diff_text.splitlines()
    patches: List[FilePatch] = []

    current_file: Optional[FilePatch] = None
    current_hunk: Optional[Hunk] = None

    def ensure_file() -> FilePatch:
        nonlocal current_file
        if current_file is None:
            current_file = FilePatch()
        return current_file

    def flush_hunk() -> None:
        nonlocal current_hunk
        if current_file is not None and current_hunk is not None:
            current_file.hunks.append(current_hunk)
        current_hunk = None

    def flush_file() -> None:
        nonlocal current_file
        flush_hunk()
        if current_file is None:
            return

        if current_file.new_path == "/dev/null":
            current_file.is_deleted_file = True

        has_payload = (
            bool(current_file.old_path)
            or bool(current_file.new_path)
            or bool(current_file.hunks)
            or current_file.is_deleted_file
            or current_file.is_binary
            or current_file.is_rename
        )
        if has_payload:
            patches.append(current_file)
        current_file = None

    for ln in lines:
        if ln.startswith("diff --git "):
            flush_file()
            parts = ln.split(maxsplit=3)
            file_obj = ensure_file()
            if len(parts) >= 3:
                file_obj.old_path = parts[2].strip()
            if len(parts) >= 4:
                file_obj.new_path = parts[3].strip()
            continue

        if ln.startswith("--- "):
            # If a new --- appears after hunks, start a new file section.
            if current_file is not None and current_file.hunks:
                flush_file()
            file_obj = ensure_file()
            file_obj.old_path = ln[4:].strip()
            continue

        if ln.startswith("+++ "):
            file_obj = ensure_file()
            file_obj.new_path = ln[4:].strip()
            if file_obj.new_path == "/dev/null":
                file_obj.is_deleted_file = True
            continue

        if ln.startswith("rename from "):
            file_obj = ensure_file()
            file_obj.is_rename = True
            file_obj.old_path = ln[len("rename from "):].strip()
            continue

        if ln.startswith("rename to "):
            file_obj = ensure_file()
            file_obj.is_rename = True
            file_obj.new_path = ln[len("rename to "):].strip()
            continue

        if ln.startswith("deleted file mode "):
            file_obj = ensure_file()
            file_obj.is_deleted_file = True
            continue

        if ln.startswith("Binary files ") or ln.startswith("GIT binary patch"):
            file_obj = ensure_file()
            file_obj.is_binary = True
            continue

        if ln.startswith("@@"):
            ensure_file()
            flush_hunk()
            current_hunk = Hunk(header=ln, lines=[])
            continue

        if current_hunk is not None:
            current_hunk.lines.append(ln)
            continue

    flush_file()
    return patches


def _fit_file_list_to_budget(paths: Sequence[str], budget_tokens: int) -> Tuple[List[str], int]:
    """
    Include as many file names as possible under budget, preserving order.
    """
    if budget_tokens <= 0:
        return [], len(paths)

    selected: List[str] = []
    used = 0
    for path in paths:
        candidate_tokens = estimate_tokens(f"- {path}\n")
        if used + candidate_tokens > budget_tokens:
            break
        selected.append(path)
        used += candidate_tokens

    omitted = max(0, len(paths) - len(selected))
    return selected, omitted


def compress_pr_diff(
    diff_text: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    buffer_tokens: int = DEFAULT_BUFFER_TOKENS,
    *,
    other_files_budget: int = DEFAULT_OTHER_FILES_BUDGET,
    deleted_files_budget: int = DEFAULT_DELETED_FILES_BUDGET,
    skip_generated: bool = True,
) -> Dict[str, Any]:
    patches = parse_unified_diff(diff_text)

    candidates: List[Tuple[int, FilePatch]] = []
    other_modified_all: List[str] = []
    deleted_files_all: List[str] = []
    skipped_files: List[str] = []

    for idx, fp in enumerate(patches):
        filename = fp.filename()
        if not filename:
            continue

        if fp.should_skip(skip_generated=skip_generated):
            skipped_files.append(filename)
            continue

        if fp.is_deleted_file or fp.new_path == "/dev/null":
            deleted_files_all.append(filename)
            continue

        filtered_hunks = fp.filtered_hunks_keep_additions()
        if not filtered_hunks:
            other_modified_all.append(filename)
            continue

        candidates.append((idx, fp))

    # Prioritize by language; preserve source order within same language bucket.
    candidates.sort(key=lambda item: (language_rank(item[1].ext()), item[0]))

    patch_token_limit = max(1, max_tokens - buffer_tokens)
    used_patch_tokens = 0
    included_patches: List[Dict[str, Any]] = []

    for _, fp in candidates:
        patch_text = fp.patch_text(filtered=True)
        if not patch_text:
            other_modified_all.append(fp.filename())
            continue

        patch_tokens = estimate_tokens(patch_text)
        if used_patch_tokens + patch_tokens <= patch_token_limit:
            included_patches.append(
                {
                    "file": fp.filename(),
                    "tokens": patch_tokens,
                    "patch": patch_text,
                }
            )
            used_patch_tokens += patch_tokens
        else:
            other_modified_all.append(fp.filename())

    other_modified_all = _dedupe_keep_order(other_modified_all)
    deleted_files_all = _dedupe_keep_order(deleted_files_all)

    other_modified_files, other_omitted = _fit_file_list_to_budget(
        other_modified_all, other_files_budget
    )
    deleted_files, deleted_omitted = _fit_file_list_to_budget(
        deleted_files_all, deleted_files_budget
    )
    
    payload = {
        "included_patches": included_patches,
        "other_modified_files": other_modified_files,
        "deleted_files": deleted_files,
        "stats": {
            "token_estimator": TOKEN_ESTIMATOR,
            "max_tokens": max_tokens,
            "buffer_tokens": buffer_tokens,
            "patch_token_limit": patch_token_limit,
            "patch_tokens_used": used_patch_tokens,
            "parsed_files_total": len(patches),
            "included_files_count": len(included_patches),
            "other_modified_files_count": len(other_modified_files),
            "deleted_files_count": len(deleted_files),
            "other_modified_files_omitted_count": other_omitted,
            "deleted_files_omitted_count": deleted_omitted,
            "skipped_files_count": len(skipped_files),
            "skip_generated": skip_generated,
            "other_files_budget_tokens": other_files_budget,
            "deleted_files_budget_tokens": deleted_files_budget,
        },
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compress a unified PR diff to fit token budgets."
    )
    parser.add_argument("--diff", required=True, help="Path to a .diff/.patch file")
    parser.add_argument("--max_tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--buffer_tokens", type=int, default=DEFAULT_BUFFER_TOKENS)
    parser.add_argument("--other_files_budget", type=int, default=DEFAULT_OTHER_FILES_BUDGET)
    parser.add_argument("--deleted_files_budget", type=int, default=DEFAULT_DELETED_FILES_BUDGET)
    parser.add_argument(
        "--include_generated",
        action="store_true",
        help="Include generated/noisy files (lockfiles, dist/, build/, node_modules/).",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Output JSON file path. If omitted, prints JSON to stdout.",
    )
    args = parser.parse_args()

    diff_text = Path(args.diff).read_text(encoding="utf-8", errors="ignore")
    payload = compress_pr_diff(
        diff_text=diff_text,
        max_tokens=args.max_tokens,
        buffer_tokens=args.buffer_tokens,
        other_files_budget=args.other_files_budget,
        deleted_files_budget=args.deleted_files_budget,
        skip_generated=not args.include_generated,
    )

    data = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(data, encoding="utf-8")
        print(f"Saved: {args.out}")
    else:
        print(data)

    print("Stats:", payload["stats"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
