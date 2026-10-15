"""Bug-fix detection from commit messages, and the attribution score shared with
the GitHub layer.

The GitHub chain (Issue <-FIXES- PR <-PART_OF- Commit) only sees fixes that went
through an issue *and* a PR that names it. Most fixes don't: on PetClinic, of the
~23 fix commits that change application methods, the chain catches 4. A commit
whose own message says it fixes something is the far more common record, and it
needs nothing but git -- so it works on any repository, whatever its tracker.

    classify("fix: normalize whitespace in owner search")  -> "conventional"
    classify("fixed lazy loading issues")                    -> "keyword"
    classify('Revert "Make jar not war"')                    -> "revert"
    classify("Fix typo in readme")                           -> None   (not a code fix)
    classify("Merge pull request #12 from x/fix-y")          -> None   (the fix itself is counted)

It is a heuristic over free text and is stated as one: a "fix" that only renames a
variable will be counted, and a real fix described as "handle empty owner list"
will not. Precision is helped by attribution (below) and by the fact that only
commits which change application methods produce edges at all -- a fix to a
README or a build file never reaches a Method.
"""

import re

# Conventional Commits: "fix: ...", "fix(scope): ...", "fix!: ...".
CONVENTIONAL_FIX_RE = re.compile(r"^\s*fix(\([^)]*\))?!?:", re.IGNORECASE)
# Types that are never a code fix, even if the subject goes on to say "fix".
CONVENTIONAL_OTHER_RE = re.compile(r"^\s*(docs?|style|test|tests|chore|ci|build)(\([^)]*\))?!?:", re.IGNORECASE)

FIX_WORD_RE = re.compile(r"\b(fix|fixes|fixed|fixing|bugfix|hotfix|bug|bugs|regression)\b", re.IGNORECASE)
# Chinese commit subjects ("库存问题修复" = stock issue fixed). CJK text has no word
# boundaries, so these match as plain substrings: 修复 fix/repair, 修正 correct,
# 解决 solve/resolve. 修改 ("modify") is deliberately absent -- it means any change.
CJK_FIX_RE = re.compile(r"修复|修正|解决")
# git's own revert subject: Revert "<original subject>"
REVERT_RE = re.compile(r'^\s*Revert\s+"')
MERGE_RE = re.compile(r"^\s*Merge\b")

# Fixes that are about text, layout or tooling rather than behaviour. "format" is
# deliberately absent: "Fix date formatting" is a behaviour bug, and a pure
# reformat touches so many methods that attribution already scores it near zero.
NON_CODE_RE = re.compile(
    r"\b(typos?|spelling|indent(ation)?|whitespace|tabs?|javadoc|comments?|"
    r"readme|docs?|documentation|links?|license|headers?|checkstyle|lint|ci|travis)\b",
    re.IGNORECASE,
)
# The same for Chinese: 文档 docs, 注释 comments, 错别字 typo, 拼写 spelling, 格式化 reformat.
CJK_NON_CODE_RE = re.compile(r"文档|注释|错别字|拼写|格式化")
QUOTED_RE = re.compile(r'"[^"]*"|“[^”]*”')
ISSUE_REF_RE = re.compile(r"(?<![\w/])#(\d+)\b")


def classify(subject):
    """'conventional' | 'keyword' | 'revert' | None for a commit subject line."""
    if not subject or MERGE_RE.match(subject):
        return None
    if REVERT_RE.match(subject):
        return "revert"
    if CONVENTIONAL_OTHER_RE.match(subject):
        return None
    # An explicit "fix:" is the author's own classification -- trusted over the
    # word filter below ("fix: normalize whitespace in owner search" is a real bug).
    if CONVENTIONAL_FIX_RE.match(subject):
        return "conventional"
    # A fix word inside quotes is someone else's subject being cited
    # ('Polish "Fix content negotiation"'), not a claim about this commit.
    unquoted = QUOTED_RE.sub(" ", subject)
    if NON_CODE_RE.search(unquoted) or CJK_NON_CODE_RE.search(unquoted):
        return None
    if FIX_WORD_RE.search(unquoted) or CJK_FIX_RE.search(unquoted):
        return "keyword"
    return None


def issue_refs(subject):
    """Issue numbers a subject names ("Fix #108 owner update" -> [108]). Merge
    subjects name a PR number, not an issue, so they yield nothing."""
    if not subject or MERGE_RE.match(subject):
        return []
    return sorted({int(n) for n in ISSUE_REF_RE.findall(subject)})


def attribution(n_methods, lines, total_lines, files_changed):
    """How much of a fix commit to attribute to one of the methods it touched:

        focus      = 1 / (methods the commit touched)        -- focused fix -> higher
        line_share = this method's changed lines / total     -- bigger part -> higher
        size       = 1 / (1 + files_changed / 10)            -- sweeping change -> lower

    A relative ranking heuristic, not a calibrated probability."""
    focus = 1.0 / n_methods
    line_share = (lines / total_lines) if total_lines else focus
    size = 1.0 / (1.0 + (files_changed or 0) / 10.0)
    return round(focus * line_share * size, 4)
