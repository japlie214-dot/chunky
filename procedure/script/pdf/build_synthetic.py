from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch

OUT = "chunky_link_test.pdf"
c = canvas.Canvas(OUT, pagesize=letter)

def header(text):
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 740, text)
    c.setFont("Helvetica", 11)

# Page 1 — real clickable link annotation (control: should work)
header("Page 1 -- Annotated hyperlink (control)")
c.drawString(72, 700, "Our investor relations page is linked below.")
c.drawString(72, 680, "Investor Relations")
c.linkURL("https://example.com/investor-relations", (72, 675, 220, 695), relative=0)
c.showPage()

# Page 2 — plain-text URL typed as text, NO annotation. This is the
# realistic case most PDF exporters produce for a URL that was never
# explicitly hyperlinked by the author.
header("Page 2 -- Plain-text URL, no clickable annotation")
c.drawString(72, 700, "For more information, visit:")
c.drawString(72, 680, "https://example.com/plain-text-only")
c.drawString(72, 660, "(This URL was typed as plain text, not inserted as a link.)")
c.showPage()

# Page 3 — multiple annotated links on one page.
header("Page 3 -- Multiple links on one page")
c.drawString(72, 700, "Annual Report")
c.linkURL("https://example.com/annual-report", (72, 695, 200, 715), relative=0)
c.drawString(72, 670, "Sustainability Report")
c.linkURL("https://example.com/sustainability", (72, 665, 240, 685), relative=0)
c.drawString(72, 640, "Contact Us")
c.linkURL("https://example.com/contact", (72, 635, 180, 655), relative=0)
c.showPage()

# Page 4 — internal (GoTo) link, not a URI action. Extraction should
# correctly EXCLUDE this, not report it as a broken/blank URL.
header("Page 4 -- Internal link (should NOT appear as a URL)")
c.bookmarkPage("page1_target")
c.drawString(72, 700, "See page 1 for details.")
c.linkAbsolute("page1_target", "page1_target", (72, 695, 200, 715))
c.showPage()

# Page 5 — a hyperlink embedded mid-paragraph, plus enough text to force
# a chunk split, to check the link survives attribution to the right chunk.
header("Page 5 -- Link embedded inside a long paragraph")
long_text = [
    "This page contains a substantial amount of body text so that the",
    "chunker's recursive character splitter is forced to divide this page",
    "into more than one chunk. Somewhere in the middle of this text there",
    "is a reference to our detailed methodology document, which is linked",
    "here for convenience and further reading by anyone who wants the",
    "complete calculation behind the headline figures reported elsewhere",
    "in this presentation and in the accompanying annual filing.",
]
y = 700
for line in long_text:
    c.drawString(72, y, line)
    y -= 18
c.drawString(72, y - 10, "Methodology")
c.linkURL("https://example.com/methodology", (72, y - 15, 180, y + 5), relative=0)
c.showPage()

# Page 6 — a markdown-worthy table, to stress table extraction alongside
# link handling (tangential but cheap to include for a "thorough" test).
header("Page 6 -- Table (no links)")
rows = [
    ("Metric", "FY2023", "FY2024"),
    ("Revenue", "1,480", "1,720"),
    ("Net Income", "190", "251"),
]
y = 700
for row in rows:
    c.drawString(72, y, row[0])
    c.drawString(250, y, row[1])
    c.drawString(350, y, row[2])
    y -= 20
c.showPage()

# Page 7 — no links at all, plain prose. Control page: LINK_BLOCK must
# stay empty here and must not leak a link from an adjacent page.
header("Page 7 -- Plain prose, zero links (control)")
c.drawString(72, 700, "This page intentionally contains no hyperlinks,")
c.drawString(72, 680, "internal or external, of any kind.")
c.showPage()

c.save()
print("built", OUT)
