"""Module C tools — payment verification & accounts.

The vision reasoning + auto-confirm/flag branching live inside settle_payment
(the "AI verification + IF/Switch" node). The screenshot is read from context,
never passed through the LLM as an argument.
"""
from __future__ import annotations

from crewai.tools import tool

from .. import context
from ..services import payments


@tool("Settle Payment")
def settle_payment_tool() -> str:
    """Verify the payment/claim screenshot attached to the current message
    (PayNow transfer or SkillsFuture claim) against the outstanding invoice
    using the vision model, then record it: on a confident match it's recorded
    as a confirmed payment (receipt issued) and, once all confirmed payments
    for the invoice sum to the full course fee, the invoice is marked complete;
    on a mismatch, flag staff; if unreadable, ask for a resend. An invoice may
    need more than one screenshot (e.g. SkillsFuture claim + PayNow) — each is
    verified independently. Returns the WhatsApp reply to relay. No input — the
    screenshot, whatsapp_id and phone come from context."""
    data, mimetype = context.media()
    return payments.process_payment(context.whatsapp_id(), context.phone(), data, mimetype)


@tool("Resend Receipt")
def resend_receipt_tool() -> str:
    """Resend every payment receipt for the latest enrollment to the email on
    record (an invoice may have more than one, e.g. a SkillsFuture claim
    receipt and a PayNow receipt). No input needed."""
    return payments.resend_receipt(context.whatsapp_id(), context.phone())


@tool("Payment Balance")
def payment_balance_tool(invoice_no: str = "") -> str:
    """Check the itemised outstanding balance — how much SkillsFuture claim
    and/or PayNow amount is still needed to fully settle an invoice.
    invoice_no is optional but STRONGLY preferred whenever it's known (e.g.
    from a SYSTEM DIRECTIVE, or already stated in the conversation) — if the
    participant has more than one enrollment, omitting it silently checks
    only the most recent one, which is wrong whenever they meant a different
    one. Leave blank only when no specific invoice is known or implied."""
    return payments.outstanding_balance_text(context.whatsapp_id(), context.phone(), invoice_no or None)
