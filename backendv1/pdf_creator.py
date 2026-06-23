"""
PDF rendering for worksheets.

Critical fix vs. the original version: the original used FPDF's built-in
"Helvetica" core font, which only supports Latin-1. Groq/LLM output very
commonly contains characters outside that range -- smart quotes ('  '),
em/en dashes (-, -), bullets (*), the multiplication sign (x), ellipses
(...), etc. -- any one of which made the original code crash with
FPDFUnicodeEncodingException and lose the worksheet entirely.

Fix: register the bundled DejaVu Sans TTF fonts (full Unicode coverage,
permissively licensed, see fonts/DEJAVU_LICENSE.txt) and additionally
normalise the text first so common "smart" punctuation renders as clean
typography rather than relying on the font alone. Any further leftover
character the font genuinely can't draw is replaced rather than allowed
to raise, so PDF generation can never crash a worksheet delivery.
"""
import os
import unicodedata

from fpdf import FPDF

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

# Characters LLMs frequently emit that have a perfectly good ASCII
# equivalent. Mapping these explicitly keeps the PDF looking clean even
# though DejaVu Sans could render the originals directly.
_PUNCTUATION_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u2026": "...",
    "\u00a0": " ",
    "\u2022": "-", "\u25cf": "-", "\u25aa": "-",
    "\u00d7": "x", "\u00f7": "/",
}


def sanitize_text(text):
    """Normalise text for clean, crash-proof PDF rendering."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(_PUNCTUATION_MAP.get(ch, ch) for ch in text)
    # Strip control characters (other than newline/tab) that occasionally
    # slip through LLM output and break PDF layout.
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or unicodedata.category(ch)[0] != "C")
    return text


class WorksheetPDF(FPDF):
    def __init__(self):
        super().__init__()
        self._unicode_font_loaded = False
        try:
            self.add_font("DejaVu", "", os.path.join(FONT_DIR, "DejaVuSans.ttf"))
            self.add_font("DejaVu", "B", os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"))
            self.add_font("DejaVu", "I", os.path.join(FONT_DIR, "DejaVuSans-Oblique.ttf"))
            self.base_font = "DejaVu"
            self._unicode_font_loaded = True
        except (RuntimeError, FileNotFoundError):
            # Fonts missing from the deployment for some reason -- fall
            # back to the core font rather than crash entirely. Text will
            # be sanitized aggressively (see _safe_text) to avoid
            # FPDFUnicodeEncodingException on this path.
            self.base_font = "Helvetica"

    def _safe_text(self, text):
        text = sanitize_text(text)
        if self._unicode_font_loaded:
            return text
        # No unicode font available: force into Latin-1-safe territory.
        return text.encode("latin-1", errors="replace").decode("latin-1")

    def header_block(self, subject, child_name, topic, generated_on):
        # Coloured banner background
        self.set_fill_color(255, 200, 60)          # sunny yellow
        self.rect(0, 0, 210, 30, style="F")        # full-width strip

        # Worksheet title inside banner
        self.set_xy(10, 4)
        self.set_font(self.base_font, "B", 17)
        self.set_text_color(60, 30, 0)
        self.cell(0, 10, self._safe_text(f"★  {subject} Worksheet  ★"), align="C",
                new_x="LMARGIN", new_y="NEXT")

        # Sub-line: child name + topic
        self.set_xy(10, 16)
        self.set_font(self.base_font, "I", 10)
        self.set_text_color(90, 50, 0)
        self.cell(0, 7, self._safe_text(f"For: {child_name}   |   Topic: {topic}"),
                align="C", new_x="LMARGIN", new_y="NEXT")

        self.ln(6)
        self.set_text_color(0, 0, 0)

        # Name / Date / Score bar
        self.set_font(self.base_font, "", 10)
        self.set_fill_color(240, 240, 255)          # very light lavender
        self.rect(10, self.get_y(), 190, 9, style="F")
        self.set_xy(12, self.get_y() + 1)
        self.cell(60, 7, "Name: _________________________", border=0)
        self.cell(60, 7, "Date: _______________", border=0)
        self.cell(60, 7, "Score: _______ / 4", border=0)
        self.ln(12)
        self.set_text_color(0, 0, 0)

    def body_text(self, text, size=12):
        self.set_font(self.base_font, "", size)
        for raw_line in text.split("\n"):
            line = raw_line.strip()
            if not line:
                self.ln(3)
                continue
            # Lightweight visual treatment for numbered activities
            # ("1.", "2)", etc.) without trying to be a full markdown parser.
            PILL_COLOURS = [
                (255, 230, 100),   # yellow  – Q1
                (200, 240, 200),   # green   – Q2
                (200, 220, 255),   # blue    – Q3
                (255, 215, 200),   # peach   – Q4
            ]
            if len(line) > 2 and line[0].isdigit() and line[1] in ".)":
                self.ln(3)
                q_idx = int(line[0]) - 1  # 0-based
                r, g, b = PILL_COLOURS[q_idx % 4]
                self.set_fill_color(r, g, b)
                self.set_font(self.base_font, "B", size)
                # Draw pill background
                x, y = self.get_x(), self.get_y()
                self.rect(x, y, 190, 7, style="F")
                self.set_xy(x, y)
                self.multi_cell(0, 7, self._safe_text(line), align="L",
                                new_x="LMARGIN", new_y="NEXT")
                self.set_font(self.base_font, "", size)
                self.set_fill_color(255, 255, 255)
            else:
                self.multi_cell(0, 6.5, self._safe_text(line), align="L", new_x="LMARGIN", new_y="NEXT")

    def footer(self):
        self.set_y(-15)
        self.set_font(self.base_font, "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")


def create_worksheet_pdf(student_text, child_name, subject, topic):
    """Builds the two-page worksheet PDF and returns the raw bytes.

    Page 1: the activities, for the child / parent to read together.
    Page 2: the answer key, clearly marked for parents only.

    This function is defensive by construction: sanitize_text() plus the
    bundled Unicode font mean arbitrary LLM output (including emoji-free
    unicode punctuation, accented names, currency symbols, etc.) cannot
    crash PDF generation the way the original Latin-1-only version could.
    """
    from datetime import date

    student_text = sanitize_text(student_text) or "No worksheet content was generated today."
    pdf = WorksheetPDF()
    pdf.set_auto_page_break(auto=True, margin=18)

    pdf.add_page()
    pdf.header_block(subject, child_name, topic, date.today().strftime("%d %b %Y"))
    pdf.body_text(student_text)
    pdf.set_auto_page_break(auto=False, margin=15)
    return bytes(pdf.output())

def create_answer_pdf(answer_key, child_name, subject, topic):
    """Builds the parent-only answer-key PDF and returns raw bytes."""
    from datetime import date
    answer_key = sanitize_text(answer_key) or "No answer key generated."

    pdf = WorksheetPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    # Simple, clean header for parent
    pdf.set_fill_color(220, 60, 60)          # red banner
    pdf.rect(0, 0, 210, 22, style="F")
    pdf.set_xy(10, 5)
    pdf.set_font(pdf.base_font, "B", 14)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 12, pdf._safe_text(f"ANSWER KEY — {subject} ({topic})"), align="C",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(8)

    pdf.set_font(pdf.base_font, "I", 9)
    pdf.set_text_color(140, 0, 0)
    pdf.cell(0, 6,
             pdf._safe_text(f"For parents only  |  {child_name}  |  "
                            f"Generated {date.today().strftime('%d %b %Y')}"),
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(6)

    pdf.body_text(answer_key, size=11)
    return bytes(pdf.output())