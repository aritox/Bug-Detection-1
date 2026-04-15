from typing import Optional


HISTORICAL_BUG_MEMORY = """
This system has a long history of regressions involving:
- save/update/persistence failures
- UI rendering issues, blank screens, crushed popups, hidden or duplicated buttons/icons
- access-right and visibility inconsistencies
- workflow / wizard / task transition failures
- grid / treegrid / popon instability
- CEH / Stored Procedure / Web Service / BRE / BRM / calculated-field trigger failures
- financial analysis instability, filtering, conversion, metadata persistence, popup/display issues
- multi-entity synchronization and wrong-context loading
- generative write-up / chat / attachment regressions

Use this history as prior risk knowledge, but only flag risks that are supported by the current diff.
""".strip()


FEW_SHOT_EXAMPLES = """
Example Historical Regression 1

Bug Title:
[MultiCombobox][Grid] Grid layout is displayed with a comma

Observed Code Pattern:
In UI rendering logic, a display value was changed from:
    return display;
to:
    return display.toString().replace(/,/g, '');

Why this matters:
This indicates the displayed value is likely an array or list being converted to a string.
Array/string conversion in UI rendering can introduce or remove comma-separated formatting unexpectedly.

Typical Risk Signal in Code:
- use of toString() in rendering code
- string replacement of commas in display output
- formatting logic inside grid / combo / renderer return values

Typical User Impact:
Grid or MultiCombobox values are rendered with unwanted commas or broken separators.

Expected Reviewer Behavior:
If a PR changes list/array-to-string conversion in UI rendering code, check for formatting regressions such as commas, missing separators, merged values, or broken display output.


Example Historical Regression 2

Bug Title:
Save button is missed in Screen/Main TAB

Observed Code Pattern:
Toolbar/button generation logic was modified in ExtJs toolbar rendering code.
The diff shows changes in button text/icon initialization and generic button creation, for example:
- conditional initialization of button text/icon
- icon/text values becoming null before rendering
- changes in screen-step / screen-id assembly that can affect which buttons are attached to which tab/screen

Why this matters:
Main-tab action buttons such as Save often depend on screen metadata, toolbar generation, component injection, and conditional rendering paths.
If that logic changes, a button may disappear even though no explicit "save" code was touched.

Typical Risk Signal in Code:
- changes in toolbar generation code
- conditional button/icon/text initialization
- screen/tab metadata or screen id concatenation changes
- generic button rendering path modified

Typical User Impact:
The Save button is missing from the main screen/tab, blocking the user from saving changes.

Expected Reviewer Behavior:
If a PR changes toolbar rendering, button injection, screen/tab metadata, or component generation logic, check whether required action buttons (especially Save) may disappear from the main tab.


Example Historical Regression 3

Bug Title:
Notification Icon is not clickable

Observed Code Pattern:
Notification-related rendering/container logic was changed.
The diff shows notification container value generation being modified, including:
- replacing formatted date handling with a generated safe string value
- changing HTML content injected into notification overlay/container
- icon CSS / icon retrieval logic made conditional in toolbar-related code

Why this matters:
Notification icons often depend on both:
1. correct icon CSS/class lookup
2. correct surrounding container/HTML rendering
A change in either can make the icon visible but not clickable, or break the clickable area.

Typical Risk Signal in Code:
- icon CSS lookup changes
- icon key/casing changes
- notification container HTML rendering changes
- dynamic span/div content changes around notification overlay
- conditional null icon assignment before rendering

Typical User Impact:
Notification icon appears but user cannot click it, or the clickable area is broken/misaligned.

Expected Reviewer Behavior:
If a PR changes icon lookup, notification HTML/container generation, or overlay markup, check for regressions where the icon renders but loses click behavior or usable interaction.


Example Historical Regression 4

Bug Title:
Redundancy of the first step

Observed Code Pattern:
Wizard navigation logic was modified in ACPWizard / toolbar step handling code.
The diff shows:
- a wizard action block commented out / removed
- changes in step index / next-step transition flow
- conditional logic around first/last steps altered
- screen list / screenUkIds concatenation changed for intermediate steps

Why this matters:
When step navigation or screen-step indexing changes, the wizard may:
- duplicate the first step
- repeat a previous step
- skip or misalign the next step
- rebuild the wrong step sequence

Typical Risk Signal in Code:
- changes in next/previous step logic
- commented-out wizard transition block
- modified first/last step conditions
- screen-step or screen-id concatenation changes

Typical User Impact:
The first step appears redundantly, navigation becomes inconsistent, or the wizard flow is broken.

Expected Reviewer Behavior:
If a PR changes wizard navigation, step indexing, or intermediate-step filtering, check for duplicated first step, repeated steps, skipped steps, and broken progression.
""".strip()


def build_main_review_prompt(
    pr_title: str,
    pr_description: str,
    changed_files: str,
    compressed_diff: str,
    ticket_context: Optional[str] = None,
    extra_context: Optional[str] = None,
) -> str:
    """
    Build the primary bug-risk focused PR review prompt.
    
    Args:
        pr_title: PR title from Azure DevOps
        pr_description: PR description body
        changed_files: List of changed files (comma-separated or formatted)
        compressed_diff: The actual diff content (likely pre-compressed)
        ticket_context: Optional ticket/issue metadata
        extra_context: Optional additional context
    
    Returns:
        Formatted prompt string for the LLM
    """
    return f"""You are an expert PR bug reviewer for a large enterprise platform.

Historical bug memory:
{HISTORICAL_BUG_MEMORY}

Goal:
- Find only strong, concrete bug risks that are directly supported by the diff.

Hard rules:
1. Output at most 3 findings.
2. Prefer 1 strong precise bug over several vague findings.
3. Each bug title must be short, explicit, and user-visible.
4. Avoid long explanations, repeated wording, praise, and style comments.
5. Every finding must reference an exact changed file and changed code element in Evidence.
6. Evidence must be directly grounded in the diff. If the evidence is weak, do not report the bug.
7. Impact must be short and user-visible.
8. Suggested tests are forbidden.
9. Output only the markdown table below, or exactly: No strong bug detected
10. Do not add introductions, summaries, bullets, code fences, or text before/after the table.

Title guidance:
- Bad: Inconsistent Combobox Rendering
- Good: Multicombobox grid values displayed with commas

Allowed Risk Type values:
- UI
- Persistence
- Workflow
- Access
- Integration
- Financial
- Data
- Other

PR Title:
{pr_title}

PR Description:
{pr_description}

Changed Files:
{changed_files}

Ticket Context:
{ticket_context or "None"}

Additional Context:
{extra_context or "None"}

Compressed Diff:
{compressed_diff}

Return exactly this format:

| Bug Title | File | Evidence | Risk Type | Impact | Confidence |
|---|---|---|---|---|---|
| ... | ... | ... | ... | ... | High/Medium/Low |

Constraints for each row:
- Bug Title: 3 to 8 words, explicit failure
- File: one repo-relative path
- Evidence: one short sentence naming the changed symbol, condition, or code path
- Risk Type: one allowed value
- Impact: one short user-visible consequence
- Confidence: High, Medium, or Low

If there is no strong bug, return exactly:
No strong bug detected"""


def build_reflection_prompt(draft_review: str, compressed_diff: str) -> str:
    """
    Build a validation/refinement prompt for the first-pass review.
    Used to filter hallucinations and strengthen evidence.
    
    Args:
        draft_review: The initial review output from the main prompt
    
    Returns:
        Formatted reflection prompt
    """
    return f"""You are a second-pass PR review validator.

Your job is to keep only the strongest findings from the draft review.

Hard rules:
1. Re-check every draft finding against the diff.
2. Delete weak, generic, duplicate, or unsupported findings.
3. Prefer fewer findings with stronger evidence.
4. Keep at most 3 findings.
5. Keep bug titles short, explicit, and user-visible.
6. Keep Evidence and Impact concise.
7. Output only the markdown table below, or exactly: No strong bug detected
8. Do not add introductions, summaries, bullets, code fences, or text before/after the table.
9. Suggested tests are forbidden.

Compressed Diff:
{compressed_diff}

Draft review:
{draft_review}

Return exactly this format:

| Bug Title | File | Evidence | Risk Type | Impact | Confidence |
|---|---|---|---|---|---|
| ... | ... | ... | ... | ... | High/Medium/Low |

If no strong finding remains after validation, return exactly:
No strong bug detected"""
