# Agent command and connector contract

The local application stores state and enforces approvals; the AI host uses its connected tools for research, mail and browser actions. It does not contain Apollo/Lusha/email API keys or simulate those providers.

Run `python3 crm.py state` to inspect local data, `python3 crm.py due` for due actions, and `python3 crm.py action ACTION --input private/payload.json` for a mutation. Alternatively pass one JSON object on stdin. Only `serve` starts a web server. CLI output can contain private correspondence: do not paste it into public logs.

| Action | Required JSON fields |
|---|---|
| `setup` | locations (array), fields (array), sender, timezone, provider (`Apollo` or `Lusha`), stages (array), followup_days (2 or 3), max_followups; BDM also product, problem, stakeholders |
| `contact` | company (normalized parent group), name, role, email, source, verified (boolean); optionally observation, problem, idea, metric, location |
| `stage` | id (contact), stage (one of the configured stages) |
| `draft` | contact_id, subject, body, kind (`initial`, `followup`, `reply`), parent_id where applicable, attachments (array of existing local file paths) |
| `approve` / `reject` | id (draft), version (integer), hash (full current content hash) |
| `revise` | id, subject, body; changed recipients or attachments require a new draft |
| `mailbox` | company, complete=true, evidence (private source/checkpoint reference) |
| `inbound` | id (contact), kind (`human_reply`, `optout`, `bounce`, `rejected`, `ooo`, `automatic`), provider_id; OOO also defer_until (UTC epoch seconds) |
| `claim` | id (draft); a new stakeholder at a contacted company also requires switch_reason |
| `result` | id, claim_token from claim, outcome (`sent` or `unknown`), provider_id for sent |
| `consent` | active (boolean), job project only; saves current geographic and field scope until revoked |
| `cv` | path (existing PDF), label, fields (array), job project only in workflow |
| `application_claim` | company, vacancy_id (stable employer ID), location and field matching consent, url (verified HTTPS), cv_id |
| `application_result` | id, state (`submitted` or `unknown`), evidence for submitted |
| `recover` | empty object; interrupted sends and applications become unknown |

## Sending transaction

1. Discover and verify a current named stakeholder with Apollo or Lusha. Separate candidate discovery, current role confirmation and work-email verification. Save evidence and credit usage locally.
2. Read Inbox and Sent, all relevant pages. Record replies/suppressions before marking mailbox review complete.
3. Generate a specific draft. Attach the correct local CV if applicable. Queue it. The returned hash covers recipient, body, subject, message type, parent and attachment hashes.
4. `python3 telegram.py notify DRAFT_ID` requests exact approval. The dashboard can also record the operator's explicit approval. An agent must never call `approve` based on its own judgment; use actual human approval of that exact version.
5. Recheck current replies, approval and attachment hashes. `claim` reserves one send atomically. Use the returned exact recipient, subject, body and attachments with the connected sender account. For a follow-up/reply, use the provider's genuine reply capability and parent message, not a fabricated `Re:` thread.
6. Immediately call `result` with the claim token and actual provider ID. If the tool times out or its outcome is unclear, call `result` with `unknown`. Do not retry the external send. “Sent” means accepted by the provider, not delivered/read.
7. Notify Telegram of the provider-confirmed outcome through the connected tool or Bot API and record receipt evidence privately. Do not describe an approval as a sent email. This bridge does not automatically send email confirmations.
8. On the next authorized active session or explicitly configured schedule, read `due`. It returns `draft_followup` or `research_alternate_stakeholder`. Drafts still require approval. A pending/rejected draft, human reply, opt-out, bounce, OOO delay or uncertain send takes precedence.

Mailbox evidence is an agent attestation; this local program cannot independently inspect a host connector's inbox. The operator and host must execute the contract truthfully. Live email/browser integrations require an end-to-end self-test in each installation. The database is not an access-control boundary against a person who can edit its files.
