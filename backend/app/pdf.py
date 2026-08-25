"""PDF engine — invoice / receipt / credit-note generation with PayNow QR.

This is the self-hosted "PDF microservice" role from the proposal (Section 5.4 /
7.1), implemented in-process with ReportLab. Each function returns the path to a
generated PDF under backend/generated/.

NOTE: the PayNow QR encodes a human-readable payment reference (UEN, amount,
invoice no) for the proof-of-concept. A production build would emit a proper
EMVCo SGQR payload — swap the payload string in _paynow_qr_png().
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .config import settings
from .utils import money

_BRAND = colors.HexColor("#1F3864")
_styles = getSampleStyleSheet()
_title = ParagraphStyle("qmTitle", parent=_styles["Title"], textColor=_BRAND, fontSize=20)
_h = ParagraphStyle("qmH", parent=_styles["Heading2"], textColor=_BRAND)
_normal = _styles["Normal"]
_small = ParagraphStyle("qmSmall", parent=_styles["Normal"], fontSize=8, textColor=colors.grey)


def _paynow_qr_png(amount: float, ref: str) -> Path:
    payload = (
        f"PayNow UEN: {settings.paynow_uen} | Payee: {settings.paynow_payee} | "
        f"Amount: {money(amount)} | Ref: {ref}"
    )
    img = qrcode.make(payload)
    out = settings.generated_dir / f"qr_{ref}.png"
    img.save(out)
    return out


def _header(elements: list) -> None:
    elements.append(Paragraph(settings.company_name, _title))
    elements.append(Paragraph(f"UEN: {settings.company_uen}", _small))
    elements.append(Spacer(1, 8 * mm))


def _doc(path: Path):
    return SimpleDocTemplate(
        str(path), pagesize=A4,
        topMargin=18 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
    )


def _party_table(enrollment: dict) -> Table:
    data = [
        ["Participant", enrollment["full_name"]],
        ["NRIC", enrollment["nric"]],
        ["Email", enrollment["email"]],
        ["Course", enrollment["course_name"]],
        ["Course Date", enrollment.get("course_date") or "TBC"],
    ]
    t = Table(data, colWidths=[40 * mm, 120 * mm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), _BRAND),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _fee_table(enrollment: dict) -> Table:
    data = [
        ["Description", "Amount"],
        ["Course Fee", money(enrollment["fee"])],
        ["Less: SkillsFuture Credit (claimable)", f"-{money(enrollment['sf_subsidy'])}"],
        ["Net Payable", money(enrollment["net_payable"])],
    ]
    t = Table(data, colWidths=[120 * mm, 40 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _BRAND),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
        ("LINEABOVE", (0, -1), (-1, -1), 0.5, _BRAND),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def generate_invoice_pdf(enrollment: dict) -> Path:
    """Tax-style invoice with fee breakdown + PayNow QR (Module B)."""
    inv_no = enrollment["invoice_no"]
    out = settings.generated_dir / f"invoice_{inv_no}.pdf"
    elements: list = []
    _header(elements)
    elements.append(Paragraph("INVOICE", _h))
    elements.append(Paragraph(f"Invoice No: <b>{inv_no}</b>", _normal))
    elements.append(Paragraph(f"Date: {datetime.now():%d %b %Y}", _normal))
    elements.append(Spacer(1, 6 * mm))
    elements.append(_party_table(enrollment))
    elements.append(Spacer(1, 6 * mm))
    elements.append(_fee_table(enrollment))
    elements.append(Spacer(1, 8 * mm))

    elements.append(Paragraph("Payment — PayNow", _h))
    elements.append(Paragraph(
        f"Scan the PayNow QR or transfer to UEN <b>{settings.paynow_uen}</b> "
        f"({settings.paynow_payee}). Reference: <b>{inv_no}</b>.", _normal))
    elements.append(Spacer(1, 3 * mm))
    qr = _paynow_qr_png(float(enrollment["net_payable"]), inv_no)
    elements.append(Image(str(qr), width=40 * mm, height=40 * mm))
    elements.append(Spacer(1, 6 * mm))
    elements.append(Paragraph(
        "SkillsFuture Credit: eligible participants may offset the fee via the "
        "MySkillsFuture portal. Submit your claim before the course start date.", _small))
    _doc(out).build(elements)
    return out


_PAYMENT_METHOD_LABELS = {
    "paynow": "PayNow",
    "skillsfuture_claim": "SkillsFuture Credit Claim",
}


def generate_receipt_pdf(enrollment: dict, payment: dict) -> Path:
    """Official receipt for ONE confirmed payment proof (Module C).

    An invoice may be settled by more than one payment proof (e.g. a
    SkillsFuture claim plus a PayNow transfer) — each gets its own receipt,
    keyed by payment['receipt_no'], not the enrollment's.
    """
    rcp_no = payment["receipt_no"]
    out = settings.generated_dir / f"receipt_{rcp_no}.pdf"
    elements: list = []
    _header(elements)
    elements.append(Paragraph("OFFICIAL RECEIPT", _h))
    elements.append(Paragraph(f"Receipt No: <b>{rcp_no}</b>", _normal))
    elements.append(Paragraph(f"Invoice No: {enrollment.get('invoice_no')}", _normal))
    elements.append(Paragraph(f"Date: {datetime.now():%d %b %Y}", _normal))
    elements.append(Spacer(1, 6 * mm))
    elements.append(_party_table(enrollment))
    elements.append(Spacer(1, 6 * mm))
    paid = payment.get("detected_amount") or 0
    ref = payment.get("reference") or "-"
    method = _PAYMENT_METHOD_LABELS.get(payment.get("payment_type"), "Payment")
    data = [
        ["Amount Received", money(paid)],
        ["Payment Method", method],
        ["Transaction Ref", ref],
        ["Status", "PAID"],
    ]
    t = Table(data, colWidths=[120 * mm, 40 * mm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TEXTCOLOR", (0, 0), (0, -1), _BRAND),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 8 * mm))
    elements.append(Paragraph("Thank you for your payment. We look forward to seeing you.", _normal))
    _doc(out).build(elements)
    return out


def generate_credit_note_pdf(enrollment: dict, credit_note: dict) -> Path:
    cn_no = credit_note["credit_note_no"]
    out = settings.generated_dir / f"credit_note_{cn_no}.pdf"
    elements: list = []
    _header(elements)
    elements.append(Paragraph("CREDIT NOTE", _h))
    elements.append(Paragraph(f"Credit Note No: <b>{cn_no}</b>", _normal))
    elements.append(Paragraph(f"Against Invoice: {enrollment.get('invoice_no')}", _normal))
    elements.append(Paragraph(f"Date: {datetime.now():%d %b %Y}", _normal))
    elements.append(Spacer(1, 6 * mm))
    elements.append(_party_table(enrollment))
    elements.append(Spacer(1, 6 * mm))
    data = [["Credit Amount", money(credit_note.get("amount") or enrollment["net_payable"])],
            ["Reason", credit_note.get("reason") or "-"]]
    t = Table(data, colWidths=[120 * mm, 40 * mm])
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 10),
                           ("TEXTCOLOR", (0, 0), (0, -1), _BRAND),
                           ("ALIGN", (1, 0), (1, -1), "RIGHT")]))
    elements.append(t)
    _doc(out).build(elements)
    return out
