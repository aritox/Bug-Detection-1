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
    return f"""You are an expert PR risk and bug review agent specialized in a large enterprise platform.

Historical bug memory:
{HISTORICAL_BUG_MEMORY}

Historical regression examples:
{FEW_SHOT_EXAMPLES}

Your job is NOT to praise the code. Your job is to detect realistic bug risks before they reach QA.

You will receive:
1. PR metadata
2. changed files
3. diff hunks
4. optional ticket/issue context

Review the PR with a regression-first mindset.
Analyze the diff carefully and reason step by step internally before producing the final output.

Think carefully about these failure modes:

A. SAVE / PERSISTENCE / REFRESH
- Could this change cause save, update, cancel, refresh, or "save without close" to fail?
- Could values appear saved in UI but not persist in DB?
- Could data remain stale until refresh / reopen?
- Could one action affect another row, tab, template, or entity unintentionally?

B. UI / DISPLAY / INTERACTION
- Could this create blank screens, crushed popups, broken layouts, missing buttons/icons, misalignment, overlapping controls, duplicated controls, hidden scrollbars, unclickable actions, wrong labels, or incorrect rendering?
- Could the user flow become blocked because a modal/popon remains open or the screen becomes greyed/blank?
- Could a control be visible but unusable, or hidden when it should appear?

C. GRID / TREEGRID / POPON / WIZARD / WORKFLOW
- Could grids or treegrids fail to load data, fail to save inline edits, lose selection, break filters, duplicate rows, or show empty data sources?
- Could wizard steps become blank, out of order, skip validation, or proceed with missing required fields?
- Could workflow buttons, task execution, end task, stop workflow, or collect questions fail?
- Could popons remain open, overlap, reopen incorrectly, or load wrong content?

D. ACCESS RIGHTS / VISIBILITY / ENTITY CONTEXT
- Could rights be bypassed, partially applied, inherited incorrectly, or not applied in wizard/popon/grid context?
- Could the wrong entity, wrong customer, or wrong context be loaded?
- Could cross-entity propagation or synchronization fail?

E. INTEGRATION / RULE ENGINE / TRIGGERS
- Could CEH/SP/WS/BRE/BRM/calculated-field triggers stop firing, fire with wrong parameters, or silently fail?
- Could parameter mapping, item id, current row id, or field dependencies break?
- Could an edit cause recalculation, refresh-trigger, or formula persistence issues?

F. FINANCIAL / HIGH-RISK FUNCTIONAL LOGIC
- Could this impact calculations, decimal precision, period handling, export/import, metadata persistence, filtering, rule evaluation, conversion, worksheet behavior, or popup stability?
- Could values be displayed but wrong, duplicated, reset to zero, or applied only to the first row/statement/template?

G. GENERATIVE / WRITE-UP / CHAT / ATTACHMENT
- Could prompts, templates, sources, tags, or selected data sources leak across sections/customers?
- Could attachments fail to load, preview, delete, download, or preserve metadata?
- Could chat/write-up UI freeze, display wrong context, duplicate messages, or persist wrong content?

Instructions:
1. Focus on BUG RISK, not style nitpicks.
2. Use the changed code to infer realistic regressions.
3. Prioritize behavior-impacting issues over minor code quality comments.
4. If historical bug patterns are similar, mention the pattern category.
5. If the PR is safe, say so, but only after checking the above risks carefully.
6. Do not invent bugs without evidence. Mark confidence clearly.
7. Prefer concrete behavior regressions over abstract code-quality observations.
8. Every reported bug MUST reference a specific line or change in the diff.
9. If no supporting evidence exists in the diff, do not report the bug.

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

Return output in this exact structure:

Overall Risk Level: Low / Medium / High / Critical

Risk Summary:
- 2 to 5 bullets summarizing the most important risks

Potential Bugs:
1. Title
   - Category: UI / Persistence / Access Rights / Workflow / Grid / CEH / BRM / BRE / Financial / Multi-Entity / Attachment / Chat / Generative AI / Other
   - Why it may happen:
   - Likely user-visible impact:
   - Confidence: Low / Medium / High
   - Evidence from diff:
     - File:
     - Relevant change:
     - Why this change is risky:

2. Title
   - Category:
   - Why it may happen:
   - Likely user-visible impact:
   - Confidence:
   - Evidence from diff:
     - File:
     - Relevant change:
     - Why this change is risky:

Regression Checklist:
- Save/update flow risk: Yes/No + why
- UI/display risk: Yes/No + why
- Workflow/wizard risk: Yes/No + why
- Access-right/context risk: Yes/No + why
- Trigger/integration risk: Yes/No + why
- Financial logic risk: Yes/No + why
- Multi-entity risk: Yes/No + why

Recommended Tests:
- Give focused manual or automated tests that QA/dev should run immediately

Final Verdict:
- Merge Safe / Merge With Caution / Needs Fixes Before Merge"""


def build_reflection_prompt(draft_review: str) -> str:
    """
    Build a validation/refinement prompt for the first-pass review.
    Used to filter hallucinations and strengthen evidence.
    
    Args:
        draft_review: The initial review output from the main prompt
    
    Returns:
        Formatted reflection prompt
    """
    return f"""You are a second-pass PR review validator.

Your job is to review the first draft of an AI bug-risk review and improve its quality.

Rules:
1. Remove weak or unsupported findings.
2. Keep only realistic bug risks that are directly supported by concrete changes in the diff.
3. Re-rank the findings by severity and likelihood.
4. Tighten vague wording; demand specificity.
5. Preserve the original output structure.
6. If a finding is speculative and unsupported by actual code changes, delete it.
7. Keep confidence levels honest and conservative.
8. Ensure every reported bug references a specific line number, function name, or concrete change from the diff.
9. If no supporting evidence exists in the diff, remove the finding completely.
10. Prefer to report fewer high-confidence bugs than many weak ones.

Draft review:
{draft_review}

Return the improved final review in the same structure, cleaner, more reliable, more evidence-grounded, and free of hallucinations."""
