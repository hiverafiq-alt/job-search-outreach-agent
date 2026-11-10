# First-run setup and repair guide

1. Start the local dashboard with `python3 crm.py serve`. Open the full private link printed in the terminal. The fragment is an access token; do not share it. In Codespaces open the private forwarded port and append the same `#token` from the launch link. The token changes when the server restarts. No dependencies are needed for the CRM.
2. Give your AI host this repository and ask it to follow `MASTER_PROMPT.md`. Complete the onboarding form; the host must confirm the user's answers rather than using the example data.
3. Connect **Apollo or Lusha**, and an email account with Inbox, Sent, read and send capabilities. Inspect permissions. First research without paid enrichment. Ask for a credit budget, provider-native work-email-only lookup and any allowed fallback. Do not enable phone, personal-email or waterfall enrichment by assumption. Cache failed lookups and successes; never repeatedly pay for the same contact. Verify role, current employer, location/remit and mailbox ownership separately.
4. Pair **Telegram before live outreach**. Create a bot through Telegram's official BotFather. Put `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and `TELEGRAM_USER_ID` in local environment secrets; send `/start` in your private bot chat. The chat ID and user ID must be the same positive ID. Obtain IDs from your own authenticated update, never from a public group. Never store tokens in the CRM, chat or repository.
5. Run `python3 telegram.py doctor`. It verifies bot identity and detects a conflicting webhook. A webhook and `getUpdates` polling cannot be used simultaneously. Do not remove someone else's webhook automatically. Stop an old listener before starting `python3 telegram.py watch`; an OS file lock prevents concurrent listeners on one machine. A second machine still requires operator coordination.
6. Test with a fictional draft addressed to **your own email**: preview, reject, request revision from the AI host, preview the revised copy, approve that exact version, complete Inbox/Sent review, claim once, send through the connected email tool, and record the provider ID. Confirm actual inbox receipt. The listener records decisions; it does not itself send email or generate revisions. The host sends only after fresh checks. During this self-test, do not research or contact real prospects.
7. Test a stale approval, a standalone “approve” message, and a different Telegram sender. None may authorize another draft. If a preview is long, read all numbered/ordered chunks before approving the final decision message. No batch approval is implied by silence.
8. Check the mailbox completely: use explicit FROM and TO searches plus company/brand SUBJECT or TEXT terms; follow every cursor, including empty continuation pages. A domain-only text search previously missed actual sent mail. Save the evidence and a pagination checkpoint. Record a completed review with the `mailbox` action only after all relevant history is reconciled. Claims expire the review after 15 minutes.
9. Recover safely after shutdown: `python3 crm.py action recover` with `{}` on stdin marks unfinished operations uncertain. Inspect actual provider history and application confirmations. Do not automatically retry an uncertain send/application. This version intentionally leaves uncertain records on hold for human reconciliation.
10. Keep Excel exports private. The web CRM's SQLite database is authoritative. `python3 crm.py export` creates `data/crm.xlsx` with Contacts, Outbox and Applications. Excel is a snapshot, not bidirectionally synchronized; edit stages in the web CRM and re-export. Back up the database while the app is stopped.

## Failures we designed around

| Observed failure pattern | Repair / prevention |
|---|---|
| Listener dies after the original session closes | Run one persistent process under an operator-approved supervisor; health-check it. Do not promise a laptop runs while asleep. |
| Two Telegram pollers steal updates | Single-machine lock plus a one-host deployment rule. |
| Generic “approved” selects the wrong email | Bind to exact preview ID, version and content/attachment hash. |
| Editing copy or replacing a CV retains approval | Edits invalidate approval; a changed attachment blocks dispatch. |
| Partial mailbox scan misses a reply | Follow all cursors; company-level pause on human replies; fresh review before claim. |
| Generic inbox or a different person is treated as a hiring lead | Verify named stakeholder and current employer; never guess an address or identity match. |
| Paid enrichment silently becomes waterfall | Native work-email-only route, explicit budget, usage ledger, no unauthorized fallback. |
| Contact-list bulk creation loses labels | Verify saved records after writes; do not assume list membership from a success response. |
| Timeouts trigger duplicate email/application | Atomic claim, provider confirmation, unknown status and no automatic retry. |
| Out-of-office message triggers a reply | Classify automatic messages silently; delay until the stated return. |
| One corporate group gets several parallel introductions | Normalize parent company; one coordinated active sequence and stakeholder cooldown. |
| CV facts drift from older correspondence | Latest user-verified facts win. Never infer a completed qualification, salary or work authorization. |
| Background job search runs under CRM-only consent | Keep application and outreach scopes separate; ask which activities to schedule. |

Official references: [Telegram Bot API](https://core.telegram.org/bots/api), [private Codespaces ports](https://docs.github.com/en/codespaces/developing-in-a-codespace/forwarding-ports-in-your-codespace).
