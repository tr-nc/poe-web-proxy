# Unresolved upstream thinking-only reply

Investigation evidence, not an implemented-feature inventory. The session lifecycle
fix is in `22a2649`; behavior is specified by `tests/test_terminal_continuation.py`,
`tests/test_active_recovery.py`, and `tests/test_rebuild.py`. Operational recovery
constraints and explicit fallback are in the README. This investigation does **not**
claim to fix Poe's reason for omitting a final answer.

## Original incident

The Pi session in `conquer-sea-battle` reported `upstream_incomplete_reply` at
2026-11-15T15:25:40.200Z. A user `continue` at 15:27:38.053Z immediately received
`session_conflict` at 15:27:38.074Z. The input/tool results had already reached Poe;
repeating their submission or executing the last tool again is not recovery.

The original binding was `uncertain`, `submitted=true`. The latest user ID, user
content hash, request marker, and reply ID match the original request. There is
no evidence that DOM answer extraction caused this incident. The latest user
input is still pending in Pi; neither its session file nor its binding was changed
by this investigation. Real chat URLs, transcripts, auth, and database copies
must remain outside Git in private files.

## Follow-up read-only observations

Rechecked the exact incident (not whichever session file happens to be newest)
using an independent authenticated diagnostic browser. The service-owned browser
was not navigated or closed. No Retry button was pressed.

- The original request's user/hash/reply still match the binding.
- `complete=true`, `generating=false`, terminal reply controls present.
- DOM answer text is empty; there is no sole JSON code block.
- After removing Thinking descendants, remaining DOM text consists of UI controls,
  not an answer.
- The matching message in Poe's `__NEXT_DATA__` has `state=complete`,
  `responsibleJob.state=completed`, and `messageStateText=null`.
- Its 874-character Markdown source consists entirely of the Thinking label,
  quoted reasoning paragraphs, and blank lines. There is no final body or code
  fence hidden in the page data. Raw reasoning was not saved in the repository
  or exposed as an assistant answer.
- The real Browser read-only recovery loop still raises `IncompleteReply` for
  that same identity after the hydration grace period. Waiting/re-reading does
  not recreate an omitted final answer.

This distinguishes an upstream completed reasoning-only reply from a lost stream,
missing renderer content, or an observed explicit provider error. It does **not**
distinguish the underlying causes of the omission (model/provider generation,
Poe output limits/cancellation, or a Poe integration defect). The completed job
and lack of an error are not proof of a particular cause or of successful billing.

## Controlled live check

Using a separate chat, browser, and private temporary binding database, sent two
short verification messages through the real Browser/session/HTTP chain with
`tool_choice=none`. Both returned a validated final body. An identical-request
retry between them returned the same response without another generation.

Total: **2 new generations, 0 client tools executed**. This establishes that the
current adapter can obtain final replies and continue in one chat; it cannot
prove that thinking-only replies will not recur, nor did it restore the original
Pi session. The original task and tool results were not replayed.

## Remaining investigation and recovery constraints

To claim a clean fix for upstream omissions, obtain a reproducible provider-level
signal or cause from Poe. Do not change extraction to expose Thinking, convert
invalid JSON into tools, inject a synthetic assistant success, click Retry, or
silently send the old task again. A provider-side failure cannot be repaired by
renaming its HTTP error.

Poe's public FAQ describes third-party model ownership and context limits, but
supplies no causal explanation or guaranteed remedy for this incident:
https://help.poe.com/hc/en-us/articles/19944206309524-Poe-FAQs
Support entry: https://help.poe.com/hc/en-us/requests/new
No support request or transcript upload has been made.

For the actual Pi session, re-read the matching session and binding before any
operation; preserve pending user input and the Pi parent/branch relationship.
The fixed normal continuation can inspect an old uncertain request and advance
only after verified rejection, under the same browser owner. This is not
permission to execute any tool a new generation requests. If a tools-forbidden
probe requires changing the old tool schema, first recover the original rejection
read-only rather than weakening the unknown-result schema check. A controlled
HTTP probe must remain consistent with client history; do not update only the
binding and claim Pi recovery. Read the installed Pi session/SDK docs before any
Pi-file changes.

If upstream evidence becomes inaccessible, use the README's explicit reviewed
rebuild procedure, preserving local history and archiving old binding evidence.
Context loss, possible prior tool effects, and new-generation cost require review.
No promise of arbitrary-fault zero duplicate billing or exactly-once external tool
execution is made.
