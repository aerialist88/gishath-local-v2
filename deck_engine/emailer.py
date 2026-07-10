"""
deck_engine/emailer.py — stage 9: delivery (PRD §2.5 / §5 step 9, extended by
PRD v4 amendment §3.3 into a two-copy nightly newsletter).

Sends the finished deck (xlsx + Moxfield .txt attached, SGD price headline,
commander image, top-5 priciest cards, last-7-decks list) on success, or a
structured error-report email on any pipeline failure — matching Trevor's
own preferred Error Report format (What failed / Error-symptom / Likely
causes / Impact / Solutions / Next step) so a 3am failure reads exactly like
the error reports Claude gives him in conversation, not a raw stack trace.

TWO SENDS on a success (PRD v4 amendment, resolved open question #2,
2026-07-03): Trevor's own copy (config.EMAIL_TO) always includes full run
diagnostics (cost/turns/cache/tools). If config.NEWSLETTER_BCC is non-empty,
a SEPARATE, clean copy — no diagnostics — goes out with friends' addresses
in Bcc (never To/Cc, so friends never see each other's addresses; Python's
smtplib.send_message() strips the Bcc header from the transmitted copy after
using it to compute the envelope recipients, per the stdlib's documented
behaviour). A failure to send the friends' copy is logged/fallback-written
but never raised past this function — Trevor's own copy having already sent
successfully is the part that must not be masked by a friends-only problem.

Uses Gmail SMTP + an App Password — deliberately NOT Gmail API/OAuth (see
config.py's Email delivery section: this runs unattended with no browser
available to complete a consent flow). One-time setup on Trevor's Mac:
    1. Enable 2-Step Verification on the sending Gmail account.
    2. Create an App Password: https://myaccount.google.com/apppasswords
    3. export DECK_ENGINE_EMAIL_FROM="you@gmail.com"
       export DECK_ENGINE_SMTP_APP_PASSWORD="xxxx xxxx xxxx xxxx"
       export DECK_ENGINE_NEWSLETTER_BCC="friend1@example.com,friend2@example.com"  # optional
   run_nightly.sh should source these from a local, gitignored .env — never
   commit the app password or real friend addresses.

If sending itself fails (bad credentials, no internet, etc.) this module
falls back to writing the report to deck_engine/logs/ so a failure is never
totally silent, even if email delivery is broken — see _write_local_fallback.
"""
from __future__ import annotations

import html as _html
import os
import smtplib
import traceback
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from . import config, pricing as pricing_mod, run_log

# Shared with export.py's palette for visual consistency between the email
# and the attached xlsx.
_ACCENT = "#6D28D9"
_TEXT = "#111827"
_MUTED = "#6B7280"
_BORDER = "#E5E7EB"
_BG_SOFT = "#F9FAFB"
_GREEN = "#15803D"
_RED = "#B91C1C"


class EmailConfigError(RuntimeError):
    """Raised when required email config (from-address / app password) is missing."""


def _require_app_password() -> str:
    if not config.EMAIL_FROM:
        raise EmailConfigError(
            "DECK_ENGINE_EMAIL_FROM is not set — set it to a Gmail address with an App "
            "Password (see deck_engine/emailer.py docstring)."
        )
    app_password = os.environ.get(config.SMTP_APP_PASSWORD_ENV, "")
    if not app_password:
        raise EmailConfigError(
            f"{config.SMTP_APP_PASSWORD_ENV} is not set — generate an App Password at "
            "https://myaccount.google.com/apppasswords and export it before running."
        )
    return app_password


def _write_local_fallback(subject: str, body: str) -> Path:
    """Last-resort: if SMTP send fails outright, write the report to disk instead of losing it."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    safe_subject = "".join(c if c.isalnum() or c in " -_" else "" for c in subject).strip()[:80]
    path = config.LOG_DIR / f"UNSENT_{timestamp}_{safe_subject}.txt"
    path.write_text(f"Subject: {subject}\n\n{body}")
    return path


def _extract_plain_text(msg: EmailMessage) -> str:
    """Best-effort plain-text extraction for the local fallback file — works whether
    msg is a single text/plain part or a multipart/mixed with a text/plain alternative."""
    if not msg.is_multipart():
        return str(msg.get_content())
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            return str(part.get_content())
    except Exception:  # noqa: BLE001 — this is already the failure path, don't raise a second one
        pass
    return "(could not extract plain-text body — see attached xlsx if this was a success email)"


def _send(msg: EmailMessage) -> None:
    try:
        app_password = _require_app_password()
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(config.SMTP_USERNAME, app_password)
            smtp.send_message(msg)
    except Exception as exc:  # noqa: BLE001 — email delivery must never silently vanish
        body = _extract_plain_text(msg)
        fallback_path = _write_local_fallback(msg["Subject"], body)
        # Re-raise so run_nightly.sh's exit code reflects the failure, but the
        # report itself is now recoverable from disk rather than lost.
        raise RuntimeError(
            f"Email send failed ({type(exc).__name__}: {exc}). Report saved to {fallback_path} instead."
        ) from exc


def _attach_files(msg: EmailMessage, xlsx_path: Path, moxfield_txt_path: Path | None) -> None:
    xlsx_data = xlsx_path.read_bytes()
    msg.add_attachment(
        xlsx_data, maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=xlsx_path.name,
    )
    if moxfield_txt_path is not None:
        txt_data = moxfield_txt_path.read_bytes()
        msg.add_attachment(txt_data, maintype="text", subtype="plain", filename=moxfield_txt_path.name)


def _commander_image_url(cache: dict, commander: str) -> str | None:
    """Scryfall image_uris from the already-loaded bulk cache — zero extra network
    calls (PRD v4 amendment §3.3). Falls back to the first face's image for
    double-faced cards, where Scryfall nests image_uris per-face instead of
    top-level. Returns None (email just omits the image) if neither is present —
    never worth failing a send over."""
    card = cache.get(commander.strip().lower())
    if card is None:
        return None
    image_uris = card.get("image_uris")
    if image_uris:
        return image_uris.get("normal") or image_uris.get("large") or image_uris.get("small")
    faces = card.get("card_faces") or []
    if faces and isinstance(faces[0], dict):
        face_uris = faces[0].get("image_uris")
        if face_uris:
            return face_uris.get("normal") or face_uris.get("large")
    return None


def _plain_budget_block(budget) -> str:
    """§3.4 budget-pass callout, plain-text. Deck content (not diagnostics) — friends
    see it too: 'this deck was made cheaper' is exactly what a newsletter reader wants
    to know. Empty string when the pass did nothing, so a clean run stays clean."""
    unpriced = list(getattr(budget, "unpriced", []) or []) if budget is not None else []
    if budget is None or (not budget.swaps_made and not budget.over_budget and not unpriced):
        return ""
    lines = []
    if budget.swaps_made:
        lines.append(f"Budget pass (cap SGD {budget.cap:.0f}/card):")
        for removed, removed_price, added, added_price, reason in budget.swaps_made:
            added_str = f"SGD {added_price:.2f}" if added_price is not None else "unpriced"
            lines.append(f"  - {removed} (SGD {removed_price:.2f}) -> {added} ({added_str}) — {reason}")
    if budget.over_budget:
        over = ", ".join(f"{c} (SGD {p:.2f})" for c, p in budget.over_budget)
        lines.append(f"OVER BUDGET, shipped flagged (no good substitute found): {over}")
    if unpriced:
        lines.append(
            f"Cap not checked for {len(unpriced)} unpriced card(s) (no trustworthy price found): "
            + ", ".join(unpriced)
        )
    if budget.synergy_note:
        lines.append(budget.synergy_note)
    return "\n" + "\n".join(lines) + "\n"


def _plain_success_body(
    deck, spend_summary: dict, validation_line: str, price_summary: dict,
    last_decks: list[str], *, include_diagnostics: bool, budget=None,
) -> str:
    """Plain-text fallback part — kept for mail clients that don't render HTML."""
    retry_note = (
        f"\nRebuilt mid-run: the first build was discarded (gameplan depended on an ability that "
        f"doesn't actually exist — \"{deck.retry_reason}\"); this is the corrected result.\n"
        if deck.retried else ""
    )
    top5_lines = "\n".join(f"  - {c}: SGD {p:.2f}" for c, p in price_summary["top_expensive"]) or "  (no pricing available)"
    unpriced_note = (
        f" ({price_summary['unpriced_count']} card(s) unpriced, excluded)" if price_summary["unpriced_count"] else ""
    )
    last_decks_block = "\n".join(f"  - {d}" for d in last_decks) or "  (this is the first run logged)"

    diagnostics = ""
    if include_diagnostics:
        tools_used = spend_summary.get("tools_used") or []
        tools_line = f"\nTools used beyond structured output: {', '.join(tools_used)}" if tools_used else ""
        gate_line = "\nSynergy gate fired this run (repair pass applied)." if getattr(deck, "synergy_gate_fired", False) else ""
        pool_line = "" if getattr(deck, "edhrec_pool_used", True) else "\nNo usable EDHREC pool tonight — built without it."
        diagnostics = f"""
---
Run cost: ${spend_summary.get('total_cost_usd', 0):.4f} · {spend_summary.get('total_turns', 0)} turns \
· {spend_summary.get('total_duration_ms', 0) / 1000:.0f}s
{tools_line}{gate_line}{pool_line}
"""

    return f"""Commander: {deck.concept.commander}
Archetype: {deck.final_archetype}
Why tonight: {deck.final_summary}
{retry_note}
Deck total: SGD {price_summary['total']:.2f}{unpriced_note}
Most expensive cards:
{top5_lines}

Early game: {deck.early_game}
Mid game: {deck.mid_game}
Late game: {deck.late_game}

Changes made in the optimize pass: {deck.changes_made or "no changes"}
{_plain_budget_block(budget)}
Validation: {validation_line}
{diagnostics}
Last 7 decks:
{last_decks_block}

Moxfield + breakdown sheet attached (xlsx + a plain Moxfield .txt import).
"""


def _html_budget_block(budget) -> str:
    """§3.4 budget-pass callout, HTML. Same visibility rule as the plain version:
    deck content, shown to friends too; invisible on a run where the pass did nothing."""
    e = _html.escape
    unpriced = list(getattr(budget, "unpriced", []) or []) if budget is not None else []
    if budget is None or (not budget.swaps_made and not budget.over_budget and not unpriced):
        return ""
    swap_rows = ""
    if budget.swaps_made:
        items = ""
        for removed, removed_price, added, added_price, reason in budget.swaps_made:
            added_str = f"SGD {added_price:.2f}" if added_price is not None else "unpriced"
            items += (
                f'<div style="font-size:13px; color:{_TEXT}; padding:2px 0; line-height:1.5;">'
                f'{e(removed)} <span style="color:{_MUTED};">(SGD {removed_price:.2f})</span>'
                f' &rarr; <strong>{e(added)}</strong> <span style="color:{_MUTED};">({e(added_str)})</span>'
                f'<br /><span style="font-size:12px; color:{_MUTED};">{e(reason)}</span></div>'
            )
        swap_rows = f"""
    <div style="margin-top:16px; padding-top:14px; border-top:1px solid {_BORDER};">
      <div style="font-size:12px; font-weight:600; color:{_MUTED}; text-transform:uppercase;
                  letter-spacing:.04em; margin-bottom:6px;">Budget pass (cap SGD {budget.cap:.0f}/card)</div>
      {items}
    </div>"""
    over_block = ""
    if budget.over_budget:
        over = ", ".join(f"{c} (SGD {p:.2f})" for c, p in budget.over_budget)
        over_block = f"""
    <div style="margin-top:12px; padding:10px 14px; background:#FEF2F2; border:1px solid #FECACA;
                border-radius:8px; font-size:13px; color:{_RED}; line-height:1.5;">
      <strong>Over budget, shipped flagged:</strong> no good substitute found for {e(over)} —
      swap manually if the price matters.
    </div>"""
    unpriced_block = ""
    if unpriced:
        unpriced_names = ", ".join(unpriced)
        unpriced_block = f"""
    <div style="margin-top:12px; padding:10px 14px; background:#FFFBEB; border:1px solid #FDE68A;
                border-radius:8px; font-size:13px; color:#92400E; line-height:1.5;">
      <strong>Cap not checked</strong> for {len(unpriced)} unpriced card(s) (no trustworthy price
      found): {e(unpriced_names)}
    </div>"""
    synergy_block = ""
    if budget.synergy_note:
        synergy_block = f"""
    <div style="margin-top:12px; padding:10px 14px; background:#FFFBEB; border:1px solid #FDE68A;
                border-radius:8px; font-size:13px; color:#92400E; line-height:1.5;">{e(budget.synergy_note)}</div>"""
    return swap_rows + over_block + unpriced_block + synergy_block


def _html_success_body(
    deck, spend_summary: dict, validation_line: str, is_valid: bool, price_summary: dict,
    last_decks: list[str], image_url: str | None, *, include_diagnostics: bool, budget=None,
) -> str:
    e = _html.escape  # noqa: E731 — short alias, used a lot below
    badge_color = _GREEN if is_valid else _RED

    image_block = ""
    if image_url:
        image_block = f"""
    <img src="{e(image_url)}" alt="{e(deck.concept.commander)}"
         style="width:100%; max-width:300px; border-radius:8px; margin-top:12px; display:block;" />"""

    top5_rows = "".join(
        f"""<tr><td style="padding:2px 0; font-size:13px; color:{_TEXT};">{e(c)}</td>
             <td style="padding:2px 0; font-size:13px; color:{_MUTED}; text-align:right;">SGD {p:.2f}</td></tr>"""
        for c, p in price_summary["top_expensive"]
    ) or f'<tr><td style="font-size:13px; color:{_MUTED};">(no pricing available)</td></tr>'

    unpriced_note = (
        f" ({price_summary['unpriced_count']} unpriced, excluded)" if price_summary["unpriced_count"] else ""
    )

    last_decks_block = "".join(
        f'<div style="font-size:12px; color:{_MUTED}; padding:1px 0;">{e(d)}</div>' for d in last_decks
    ) or f'<div style="font-size:12px; color:{_MUTED};">(this is the first run logged)</div>'

    diagnostics_block = ""
    if include_diagnostics:
        tools_used = spend_summary.get("tools_used") or []
        tools_used_line = (
            f'<div style="margin-top:4px; font-size:12px; color:{_MUTED}; text-align:center;">'
            f'Tools used beyond structured output: {e(", ".join(tools_used))}</div>' if tools_used else ""
        )
        gate_note = (
            f'<div style="margin-top:4px; font-size:12px; color:{_MUTED}; text-align:center;">'
            f'Synergy gate fired this run (repair pass applied).</div>'
            if getattr(deck, "synergy_gate_fired", False) else ""
        )
        pool_note = (
            "" if getattr(deck, "edhrec_pool_used", True) else
            f'<div style="margin-top:4px; font-size:12px; color:{_MUTED}; text-align:center;">'
            f'No usable EDHREC pool tonight — built without it.</div>'
        )
        diagnostics_block = f"""
  <div style="margin-top:14px; font-size:12px; color:{_MUTED}; text-align:center;">
    Run cost ${spend_summary.get('total_cost_usd', 0):.4f}
    &nbsp;·&nbsp; {spend_summary.get('total_turns', 0)} turns
    &nbsp;·&nbsp; {spend_summary.get('total_duration_ms', 0) / 1000:.0f}s
  </div>
  {tools_used_line}{gate_note}{pool_note}"""

    def _section(label: str, text: str, accent: str) -> str:
        return f"""
        <tr><td style="padding:10px 0 2px 0;">
          <div style="border-left:3px solid {accent}; padding-left:12px;">
            <div style="font-size:12px; font-weight:600; color:{accent}; text-transform:uppercase;
                        letter-spacing:.04em; margin-bottom:3px;">{e(label)}</div>
            <div style="font-size:14px; color:{_TEXT}; line-height:1.5;">{e(text)}</div>
          </div>
        </td></tr>"""

    retry_block = ""
    if getattr(deck, "retried", False):
        retry_block = f"""
    <div style="margin-top:12px; padding:10px 14px; background:#FFFBEB; border:1px solid #FDE68A;
                border-radius:8px; font-size:13px; color:#92400E; line-height:1.5;">
      <strong>Rebuilt mid-run:</strong> the first build was discarded — its gameplan depended on an
      ability that doesn't actually exist ("{e(deck.retry_reason)}"). This is the corrected result.
    </div>"""

    return f"""\
<html><body style="margin:0; padding:0; background:{_BG_SOFT};">
<div style="max-width:640px; margin:0 auto; padding:24px; font-family:-apple-system,Helvetica,Arial,sans-serif;">

  <div style="background:#ffffff; border:1px solid {_BORDER}; border-radius:10px; padding:24px;">
    <div style="font-size:11px; font-weight:600; color:{_ACCENT}; text-transform:uppercase; letter-spacing:.06em;">
      Tonight's EDH Deck
    </div>
    <div style="font-size:22px; font-weight:700; color:{_TEXT}; margin-top:4px;">{e(deck.concept.commander)}</div>
    <div style="font-size:14px; color:{_MUTED}; margin-top:2px;">{e(deck.final_archetype)}</div>
    <div style="font-size:20px; font-weight:700; color:{_ACCENT}; margin-top:8px;">
      SGD {price_summary['total']:.2f}<span style="font-size:12px; font-weight:400; color:{_MUTED};">{e(unpriced_note)}</span>
    </div>
    {image_block}

    <div style="margin-top:14px; padding:12px 14px; background:{_BG_SOFT}; border-radius:8px;
                font-size:14px; color:{_TEXT}; line-height:1.5;">
      {e(deck.final_summary)}
    </div>
    {retry_block}

    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      {_section("Early game", deck.early_game, "#1D4ED8")}
      {_section("Mid game", deck.mid_game, "#B45309")}
      {_section("Late game", deck.late_game, "#15803D")}
    </table>

    <div style="margin-top:16px; padding-top:14px; border-top:1px solid {_BORDER};">
      <div style="font-size:12px; font-weight:600; color:{_MUTED}; text-transform:uppercase;
                  letter-spacing:.04em; margin-bottom:6px;">Most expensive cards</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{top5_rows}</table>
    </div>

    <div style="margin-top:16px; padding-top:14px; border-top:1px solid {_BORDER};">
      <div style="font-size:12px; font-weight:600; color:{_MUTED}; text-transform:uppercase;
                  letter-spacing:.04em; margin-bottom:3px;">Changes made in the optimize pass</div>
      <div style="font-size:14px; color:{_TEXT}; line-height:1.5;">{e(deck.changes_made or "no changes")}</div>
    </div>
    {_html_budget_block(budget)}

    <div style="margin-top:16px; display:flex;">
      <span style="display:inline-block; padding:3px 10px; border-radius:999px; font-size:12px;
                   font-weight:700; color:#ffffff; background:{badge_color};">
        Validation: {e(validation_line)}
      </span>
    </div>

    <div style="margin-top:16px; padding-top:14px; border-top:1px solid {_BORDER};">
      <div style="font-size:12px; font-weight:600; color:{_MUTED}; text-transform:uppercase;
                  letter-spacing:.04em; margin-bottom:4px;">Last 7 decks</div>
      {last_decks_block}
    </div>
  </div>
  {diagnostics_block}
  <div style="margin-top:8px; font-size:12px; color:{_MUTED}; text-align:center;">
    Moxfield + breakdown sheet attached (xlsx + a plain Moxfield .txt import).
  </div>
</div>
</body></html>"""


def send_success_email(
    *, deck, xlsx_path: Path, spend_summary: dict, pricing, cache: dict,
    moxfield_txt_path: Path | None = None, budget=None,
) -> None:
    """Sends tonight's deck. Two sends when config.NEWSLETTER_BCC is non-empty
    (PRD v4 amendment §3.3): Trevor's own full-diagnostics copy always goes out
    first; the friends' clean copy is attempted second and never allowed to raise
    past this function (a broken newsletter send must not look like a broken
    pipeline run — Trevor's own copy already succeeded by that point)."""
    all_cards = [deck.concept.commander] + deck.cards
    price_summary = pricing_mod.deck_price_summary(pricing, all_cards)
    issue_number = run_log.successful_run_count() + 1  # +1: this run, about to be logged as a success by run.py
    last_decks = run_log.recent_deck_lines(n=7)
    image_url = _commander_image_url(cache, deck.concept.commander)
    subject = f"EDH Nightly #{issue_number} — {deck.concept.commander} (SGD {price_summary['total']:.2f})"

    validation_line = (
        "PASSED"
        if deck.validation.is_valid
        else "FAILED — this should not happen if you're reading this; check the deck carefully"
    )

    # --- Trevor's own copy: full diagnostics ---
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.EMAIL_FROM
    msg["To"] = config.EMAIL_TO
    msg.set_content(_plain_success_body(
        deck, spend_summary, validation_line, price_summary, last_decks,
        include_diagnostics=True, budget=budget,
    ))
    msg.add_alternative(
        _html_success_body(
            deck, spend_summary, validation_line, deck.validation.is_valid, price_summary,
            last_decks, image_url, include_diagnostics=True, budget=budget,
        ),
        subtype="html",
    )
    _attach_files(msg, xlsx_path, moxfield_txt_path)
    _send(msg)

    # --- Friends' newsletter copy: clean, Bcc, best-effort ---
    if config.NEWSLETTER_BCC:
        friend_msg = EmailMessage()
        friend_msg["Subject"] = subject
        friend_msg["From"] = config.EMAIL_FROM
        friend_msg["To"] = config.EMAIL_FROM  # visible "To" is the sender; real recipients are Bcc-only
        friend_msg["Bcc"] = ", ".join(config.NEWSLETTER_BCC)
        friend_msg.set_content(_plain_success_body(
            deck, spend_summary, validation_line, price_summary, last_decks,
            include_diagnostics=False, budget=budget,
        ))
        friend_msg.add_alternative(
            _html_success_body(
                deck, spend_summary, validation_line, deck.validation.is_valid, price_summary,
                last_decks, image_url, include_diagnostics=False, budget=budget,
            ),
            subtype="html",
        )
        _attach_files(friend_msg, xlsx_path, moxfield_txt_path)
        try:
            _send(friend_msg)
        except Exception as exc:  # noqa: BLE001 — a broken newsletter send must never take down Trevor's own copy
            _write_local_fallback(f"NEWSLETTER_{subject}", f"Newsletter Bcc send failed: {exc}")


def send_error_email(
    *,
    what_failed: str,
    error_symptom: str,
    likely_causes: list[str],
    impact: str,
    options: list[tuple[str, str]],
    next_step: str,
    run_id: str = "",
    appendix: str = "",
) -> None:
    """Sends a structured Error Report email — same shape as Trevor's own preferred format.

    options: list of (label, description); the FIRST entry is treated as the
    recommended option (labelled accordingly), matching "rank, don't just list" (PRD/CLAUDE.md).
    """
    msg = EmailMessage()
    msg["Subject"] = f"Nightly Deck Engine — run failed{f' ({run_id[:8]})' if run_id else ''}"
    msg["From"] = config.EMAIL_FROM
    msg["To"] = config.EMAIL_TO

    causes_text = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(likely_causes))
    options_text = "\n".join(
        f"**Option {chr(65 + i)}{' — recommended' if i == 0 else ''}:** {desc}"
        for i, (_label, desc) in enumerate(options)
    )

    body = f"""## Error Report

**What failed:** {what_failed}
**Error / symptom:** {error_symptom}
**Likely cause(s):**
{causes_text}
**Impact:** {impact}

## Solutions

{options_text}

## Next step
{next_step}
"""
    if appendix:
        body += f"\n---\n{appendix}\n"

    msg.set_content(body)
    _send(msg)


def send_error_from_exception(exc: Exception, *, stage: str, run_id: str = "") -> None:
    """Fallback for unanticipated exceptions — wraps the traceback into the same Error Report shape."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    send_error_email(
        what_failed=f"Nightly deck-engine pipeline, stage: {stage}",
        error_symptom=f"{type(exc).__name__}: {exc}",
        likely_causes=[
            "Unhandled exception in the pipeline — see the traceback appendix below for the exact location.",
            "A dependency (gishath-local-v2 app, Scryfall cache, claude CLI, SMTP) was unavailable "
            "or its contract changed since this was last verified.",
        ],
        impact="No deck was generated or delivered for tonight's run. No partial or unvalidated deck was sent — "
               "the pipeline fails closed, per PRD §2.5.",
        options=[
            ("Re-run manually", "Fix the root cause below and re-run `caffeinate ./run_nightly.sh` by hand."),
            (
                "Inspect run state",
                f"Check deck_engine/logs/spend_log.jsonl and deck_engine/state/run_log.json "
                f"for run_id={run_id or 'unknown'} to see how far the pipeline got before failing.",
            ),
        ],
        next_step="Read the traceback appendix below, fix the root cause, and re-run manually before "
                  "trusting the next scheduled run.",
        run_id=run_id,
        appendix=f"Traceback:\n{tb}",
    )
