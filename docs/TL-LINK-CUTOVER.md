# TL link cutover — signed links with a 60-day expiry (Phase 3, PR A)

**Status: PROPOSAL. Nothing here has been done in Production.** The cutover, any link issue or
rotation, and the legacy sunset date each need separate, explicit approval.

## What PR A changes, and what it doesn't

- **On deploy, no link changes.** `app/tl_links.py` ships with `ISSUE_SIGNED_LINKS = False` and
  `LEGACY_LINK_SUNSET = None`.
  - Every link already sent keeps working exactly as today, with case-only access.
  - Re-sending (Link, Email, DingTalk, TL links download) returns the stored legacy link unchanged.
- **The only visible change:** a bad link now shows one fixed sentence, *"This link isn't valid — ask
  your HRBP for a new link."*, instead of the old wording.
- **Signed links** look like `v1.<random>.<issued_at>.<signature>`.
  - They can be created only after the cutover. Before it, only tests create them.
  - A signed link is valid until `issued_at + 60 × 24 h` (UTC). After that the TL sees only
    *"Link expired — ask your HRBP for a new link."*
  - The signing key is derived from the existing `TOKEN_ENC_KEY`. It uses HKDF-SHA256 with the
    label `attendance-verification/tl-link-signing/v1`, so there is no new secret and no config
    change.
- **Database:** each TL still has one stored link (`access_token_hash` + `access_token_enc`), and
  issuing a new link replaces it. There's no schema change.

## Cutover procedure (for approval)

1. **Cutover commit.** A reviewed commit sets `ISSUE_SIGNED_LINKS = True` and
   `LEGACY_LINK_SUNSET = <cutover date + 14 days, 00:00 UTC>`. Merging it is a deploy (merge =
   deploy), so it needs its own approval.
2. **Re-issue links.** After deploy, HRBP re-issues links through the normal TL links tab, using the
   per-TL Link button or the download, email or DingTalk buttons. Each press replaces that TL's
   legacy link with a signed one, and the old link stops working. Every one of these buttons reports
   newly issued links. Sending real messages needs explicit approval for that send.
3. **Transition.** Until the sunset, a TL who still holds only a legacy link keeps **case-only**
   access. A legacy link **never** opens the team attendance view (PR C); only a valid signed link
   can.
4. **Sunset.** From `LEGACY_LINK_SUNSET`, any legacy link reads "expired". HRBP re-issues to anyone
   who asks.
5. **Routine after cutover.** A signed link stays the same when re-sent until it expires. Once
   expired, a re-send or download issues a fresh signed link automatically (approved routine). Rotate
   still revokes a link immediately.

**Proposed sunset:** 14 days after the cutover deploy. This is a proposal; the date is fixed only
when the cutover is approved.

## Risks and limits

- **Mid-period lockout.** A TL whose link expires mid-period is locked out until HRBP re-sends.
  60 days covers two attendance periods.
- **Bulk sends replace links.** After the cutover, a bulk send or download can replace many links at
  once. It replaces only links that are legacy, expired, missing, unreadable (e.g. after a key
  change) or malformed.
- **Policy check.** The sunset can only be set together with `ISSUE_SIGNED_LINKS = True`, and as a
  timezone-aware UTC datetime. `app/tl_links.py` refuses anything else at import.
- **Near-expiry links.** A link with little time left is still re-sent unchanged and can expire before
  the TL opens it. HRBP re-sends again in that case.
- **Key rotation.** Changing `TOKEN_ENC_KEY` makes every signed link invalid, because the signing key
  is derived from it, and every stored link unreadable, so new links are minted on re-send. Treat a
  key change as a full re-issue.
