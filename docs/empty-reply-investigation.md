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

## New incident: 2026-11-15T16:45:20.700Z

A separate `conquer-sea-battle` session reproduced the terminal-empty error after
successful tool turns. The service process had started before `22a2649` and was
still running old code; a Git push had not deployed the fix. Its binding remained
`uncertain` without generation-completion evidence. This explains stale recovery
behavior, not why the provider omitted final output.

Read-only Browser recovery verified this new request's marker, user ID/hash, and
reply ID and reproduced `IncompleteReply`. The stored 853-character source again
consists entirely of Thinking, with `state=complete`, job `completed`, no error
text, and no final code block.

A new confirmed transport defect was found in the same message: only two images
were intentionally uploaded, but Poe automatically fetched eight reference URLs
inside the serialized tool result as HTML attachments (including MB-sized Godot
documentation). The user message has ten attachments. Poe's message Info UI shows
384,786 input tokens, 913 output tokens, and 43,258 points ($1.31). These numbers
are observed for this message, not a new token-accounting contract. The proxy's
plain JSON serialization allowed data URLs to cause unsolicited page retrieval,
context expansion, and extra cost.

A controlled live A/B with the same public `example.com` reference and
`tool_choice=none` established causality for URL auto-fetch: raw serialization
created one HTML attachment; JSON string punctuation escapes created none, and
both replies preserved the exact URL after decoding. The fix escapes dots and
slashes only within serialized JSON strings; decimal numbers and decoded values
remain unchanged. Tests use actual HTTP prompt serialization and exact JSON
round-trip assertions. Transcript hashes remain based on decoded canonical data,
so old bindings can still be recovered without rewriting their history.

The A/B generated two short replies and executed no client tools. Both had final
bodies: it proves the auto-fetch fix, **not** that unsolicited attachments caused
the reasoning-only termination. The root cause of the latter remains unconfirmed
without provider stop-reason evidence. No automatic replay, fallback answer, or
Thinking-to-answer conversion is justified by this finding.

### Deployment and original session recovery

The URL-serialization fix is `ccc726d`. After confirming the service-owned page
was blank and not generating, backed up bindings privately and restarted the local
systemd service at 2026-11-16 00:59:27 CST to load the latest code. API readiness
passed. No live generation was interrupted.

Pi's SessionManager and provider serialization reproduced the exact failing
request: history hashes, context hash, and request digest matched the binding.
Submitting that identical request to the updated service performed read-only
recovery and durably recorded terminal failure, preserving its chat and input
cursor. No prompt, attachment, or tool result was sent again.

A new explicit tools-forbidden recovery-verification user message then returned
HTTP 200 with a real final body in the same chat. The answer acknowledged the
existing results and described remaining validation work; it did not execute it.
Both the verification input and actual assistant answer were appended to the
original Pi session via SessionManager. Every byte of its prior history remains
an unchanged prefix; errors and tool execution records were not removed. A final
Pi-provider serialization plus a next-turn dry-run passed the binding's normal
history-prefix check and contained no attachments to replay. The binding is ready.
An already-open Pi process must reload/resume this specific session to pick up the
appended entries before sending another prompt.

Follow-up total: three controlled new generations (two URL A/B probes and one
original-session verification), zero client tools executed. Private backups and
captured requests stay outside Git. This demonstrates recovery and prevention of
new automatic page attachments; it does not retroactively remove the old pages
from Poe history or prove the provider's final-omission cause is fixed.

## Supervised task continuation

After the user exited the competing Pi process and explicitly requested supervision
through completion, resumed the same session with normal project tools. The game
UI task finished with a real final assistant message and local commit `a178653` in
`conquer-sea-battle`. Its visual report is `docs/reports/pixel-ui/index.html`.
No game commits were pushed. The session binding ended `ready`; all eight supervised
Pi invocations exited. Prior tool results and failures remain in the session.

Three more proxy defects were confirmed and fixed:

- A single valid JSON tool envelope preceded by ordinary commentary was rejected
  because the extractor required an otherwise empty wrapper. In the actual failed
  reply the code block was valid and the residual 24 characters were model prose,
  not UI. `d713a32` preserves inert commentary while accepting only a single JSON
  block, still rejecting competing apparent envelopes and invalid tools/arguments.
  Chromium extraction tests and HTTP delivery tests cover the boundary.
- The same terminal rejection returned 502. Pi's outer retry classifier receives
  only the assistant error string and retries 502 regardless of the SDK's
  `x-should-retry: false` header. Terminal invalid protocol now returns 422. A real
  installed-Pi classifier check showed old status retryable, new status not retryable;
  SDK/HTTP tests also pass. Genuine browser/transport 5xx behavior is unchanged.
- Long continuation later forgot client tools and claimed only a Poe-native web
  search tool existed. The continuation prompt depended on earlier tool declarations.
  `f675a74` sends the current client-tool definitions and ownership instructions on
  every delta, without replaying old transcript messages. The next actual session
  turn resumed client tool use. This reduces protocol drift; prompts cannot guarantee
  arbitrary model adherence.

Supervision also encountered genuine long-generation timeouts, which recovered by
reading the existing page, and a browser-command/cleanup failure before send, which
preserved the prior ready binding and required service/browser restart. The precise
browser-runtime hang cause remains unknown; quarantine was not disabled. Local
systemd now passes `--timeout 600` for coding output; raising this deadline is an
operational adjustment, not a fix for thinking-only termination.

Thinking-only failures **recurred** even after URL escaping and the longer timeout.
Each confirmed failure was followed only by an explicit new input, not automatic
replay. This disproves any claim that those fixes eliminate provider final-body
omissions. Their underlying provider/Poe cause still lacks stop-reason evidence.

The completed UI task passed real GPU-window acceptance on RTX 3060 Ti (desktop,
compact, portrait), reported no script errors, and had UI/shop behavior checks.
An incidental failing E-interaction test was traced to deliberate game input
suspension after desktop focus loss. The final test acquires and checks actual
window focus without changing game focus policy or deleting interaction assertions.
The report's seven images loaded with no horizontal overflow at 1280 and 390 widths;
Pi inspected the rendered narrow screenshot. Pre-existing sky/report changes and
unrelated generated UID files remain outside the task commit; no cleanup removed
them. One upstream OFL license whitespace warning was preserved in the game commit;
it does not imply a game behavior failure.

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
