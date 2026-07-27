#!/usr/bin/env python3
"""
procedure/script/make_dummy_pdf.py
Generate a small but realistic dummy PDF used for end-to-end testing of
the Chunky ingestion pipeline.

Output: procedure/script/pdf/fy2024-tbk-investor-presentation.pdf

The PDF contains:
  * A title page with the company name and fiscal-year header.
  * Several content pages with tables, headings, and body text — enough
    structure to exercise AI_PARSE_DOCUMENT, Vision, and surgical-range
    flows without being huge.
  * A final disclaimer page.

Run:
    python3 procedure/script/make_dummy_pdf.py
"""
from __future__ import annotations
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)


OUTPUT = Path(__file__).resolve().parent / "pdf" / "fy2024-tbk-investor-presentation.pdf"


def _styles():
    base = getSampleStyleSheet()
    base.add(ParagraphStyle(name="CoverTitle", fontName="Helvetica-Bold",
                            fontSize=28, leading=34, alignment=1, spaceAfter=18))
    base.add(ParagraphStyle(name="CoverSub", fontName="Helvetica",
                            fontSize=14, leading=18, alignment=1, textColor=colors.grey))
    base.add(ParagraphStyle(name="SectionH", fontName="Helvetica-Bold",
                            fontSize=16, leading=20, spaceBefore=18, spaceAfter=10))
    base.add(ParagraphStyle(name="Body", fontName="Helvetica",
                            fontSize=11, leading=15, spaceAfter=8))
    return base


def _title_page(s):
    return [
        Spacer(1, 2.5 * inch),
        Paragraph("TBK Holdings, Inc.", s["CoverTitle"]),
        Paragraph("Fiscal Year 2024 Investor Presentation", s["CoverSub"]),
        Spacer(1, 0.5 * inch),
        Paragraph("Forward-looking statements are subject to risks and uncertainties. "
                  "See final page for full disclaimer.", s["CoverSub"]),
        PageBreak(),
    ]


def _financials_page(s):
    data = [
        ["Metric (USD millions)", "FY2022", "FY2023", "FY2024", "YoY %"],
        ["Total Revenue",         "1,250",  "1,480",  "1,720",  "+16.2%"],
        ["Gross Profit",          "  625",  "  760",  "  905",  "+19.1%"],
        ["Operating Income",      "  188",  "  248",  "  318",  "+28.2%"],
        ["Net Income",            "  142",  "  190",  "  251",  "+32.1%"],
        ["Diluted EPS",           " $1.42", " $1.90", " $2.51", "+32.1%"],
    ]
    t = Table(data, colWidths=[2.2*inch, 1.0*inch, 1.0*inch, 1.0*inch, 1.0*inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, -1), 10),
        ("ALIGN",      (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN",      (0, 0), (0, -1), "LEFT"),
        ("GRID",       (0, 0), (-1, -1), 0.5, colors.HexColor("#BFBFBF")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
            [colors.white, colors.HexColor("#F2F2F2")]),
    ]))
    return [
        Paragraph("Financial Highlights", s["SectionH"]),
        Paragraph(
            "TBK Holdings delivered record financial performance in fiscal year 2024. "
            "Total revenue grew 16.2% year-over-year, driven by strong demand in the "
            "Platform Services segment and the full-year contribution of the Atlantic "
            "acquisition closed in Q3 2023. Operating margin expanded 180 basis points "
            "to 18.5%, reflecting disciplined cost management and operating leverage.",
            s["Body"],
        ),
        Spacer(1, 0.2 * inch),
        t,
        PageBreak(),
    ]


def _segment_page(s):
    data = [
        ["Segment",            "Revenue", "Operating Income", "Customers"],
        ["Platform Services",  "    980", "           220",   "    1,420"],
        ["Hardware Solutions", "    490", "            62",   "      880"],
        ["Professional Svc.",  "    180", "            24",   "      510"],
        ["Licensing",          "     70", "            12",   "    2,150"],
    ]
    t = Table(data, colWidths=[2.4*inch, 1.2*inch, 1.6*inch, 1.2*inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2E7D32")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, -1), 10),
        ("ALIGN",      (1, 0), (-1, -1), "RIGHT"),
        ("GRID",       (0, 0), (-1, -1), 0.5, colors.HexColor("#BFBFBF")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
            [colors.white, colors.HexColor("#E8F5E9")]),
    ]))
    return [
        Paragraph("Segment Performance", s["SectionH"]),
        Paragraph(
            "Platform Services remains the company's growth engine, contributing 57% "
            "of consolidated revenue and 69% of operating income. The Hardware "
            "Solutions segment returned to growth in the second half of FY2024 as "
            "supply-chain constraints eased. Professional Services and Licensing "
            "continue to provide stable, high-margin recurring revenue.",
            s["Body"],
        ),
        Spacer(1, 0.2 * inch),
        t,
        PageBreak(),
    ]


def _outlook_page(s):
    return [
        Paragraph("FY2025 Outlook", s["SectionH"]),
        Paragraph(
            "For fiscal year 2025, management expects total revenue in the range of "
            "$1.95B to $2.05B, representing growth of 13% to 19% over FY2024. "
            "Operating margin is expected to expand another 100 to 150 basis points "
            "as the company scales Platform Services and completes the integration "
            "of the Atlantic business.",
            s["Body"],
        ),
        Paragraph(
            "Capital allocation priorities remain unchanged: (1) reinvest in the "
            "business, (2) pursue tuck-in acquisitions, and (3) return excess cash "
            "to shareholders via dividends and buybacks. The board approved a 12% "
            "increase to the quarterly dividend, effective Q1 FY2025.",
            s["Body"],
        ),
        PageBreak(),
    ]


def _disclaimer_page(s):
    return [
        Paragraph("Disclaimer", s["SectionH"]),
        Paragraph(
            "This presentation contains forward-looking statements within the meaning "
            "of the Private Securities Litigation Reform Act of 1995. Such statements "
            "are based on current expectations and are subject to risks and "
            "uncertainties that could cause actual results to differ materially. "
            "TBK Holdings undertakes no obligation to update these statements.",
            s["Body"],
        ),
        Paragraph(
            "This document is provided for informational purposes only and does not "
            "constitute an offer to sell or a solicitation of an offer to buy any "
            "security. Past performance is not indicative of future results.",
            s["Body"],
        ),
    ]


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(OUTPUT), pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.9 * inch, bottomMargin=0.9 * inch,
        title="FY2024 TBK Investor Presentation",
        author="TBK Holdings, Inc.",
    )
    s = _styles()
    story = []
    story += _title_page(s)
    story += _financials_page(s)
    story += _segment_page(s)
    story += _outlook_page(s)
    story += _disclaimer_page(s)
    doc.build(story)
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
