"""Render the Markdown sources in sample-docs/src into the upload formats
(PDF via reportlab, DOCX via a minimal OOXML writer, MD as-is).
Usage: PYTHONPATH=<dir with reportlab> python3 sample-docs/build_samples.py"""
import os, re, shutil, zipfile
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
SRC, OUT = os.path.join(HERE, "src"), os.path.join(HERE, "upload")
FORMATS = {
    "gas-leak-emergency-response.md": "pdf",
    "ppe-and-field-safety-policy.md": "pdf",
    "distribution-maintenance-protocols.md": "pdf",
    "regulatory-compliance-guide.md": "docx",
    "employee-policies-handbook.md": "md",
}
TITLES = {
    "gas-leak-emergency-response.md": "GS-EM-101 Gas Leak and Emergency Response",
    "ppe-and-field-safety-policy.md": "GS-SF-210 PPE and Field Safety Policy",
    "distribution-maintenance-protocols.md": "GS-MT-150 Distribution Maintenance Protocols",
    "regulatory-compliance-guide.md": "GS-RC-300 Regulatory Compliance Guide",
    "employee-policies-handbook.md": "HR-GO-400 Employee Policies",
}

def blocks(md):
    for line in md.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("# "): yield "h1", line[2:]
        elif line.startswith("## "): yield "h2", line[3:]
        elif re.match(r"^\d+\. ", line): yield "num", line
        elif line.startswith("- "): yield "bullet", line[2:]
        else: yield "p", line

def to_pdf(md, path, title):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    ss = getSampleStyleSheet()
    styles = {"h1": ss["Title"], "h2": ss["Heading2"], "p": ss["BodyText"],
              "num": ParagraphStyle("num", parent=ss["BodyText"], leftIndent=14),
              "bullet": ParagraphStyle("b", parent=ss["BodyText"], leftIndent=14, bulletIndent=4)}
    story = []
    for kind, text in blocks(md):
        story.append(Paragraph(escape(text), styles[kind], bulletText="•" if kind == "bullet" else None))
        if kind == "h1": story.append(Spacer(1, 6))
    def footer(c, d):
        c.setFont("Helvetica", 8)
        c.drawString(inch * .75, inch * .5, f"{title} · Fictional training sample · Page {d.page}")
    SimpleDocTemplate(path, pagesize=letter, title=title, author="Training demo").build(story, onFirstPage=footer, onLaterPages=footer)

def to_docx(md, path):
    def para(text, style=None):
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
    body = []
    for kind, text in blocks(md):
        body.append(para(text, {"h1": "Title", "h2": "Heading1", "bullet": "ListBullet"}.get(kind)))
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {W}><w:body>{"".join(body)}</w:body></w:document>'
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {W}>'
              + "".join(f'<w:style w:type="paragraph" w:styleId="{s}"><w:name w:val="{n}"/><w:rPr>{r}</w:rPr></w:style>'
                        for s, n, r in [("Title", "Title", '<w:b/><w:sz w:val="40"/>'), ("Heading1", "heading 1", '<w:b/><w:sz w:val="30"/>'),
                                        ("ListBullet", "List Bullet", "")]) + "</w:styles>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/_rels/document.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", styles)

os.makedirs(OUT, exist_ok=True)
for name, fmt in FORMATS.items():
    md = open(os.path.join(SRC, name), encoding="utf-8").read()
    base = TITLES[name]
    dest = os.path.join(OUT, f"{base}.{fmt}")
    if fmt == "pdf": to_pdf(md, dest, base)
    elif fmt == "docx": to_docx(md, dest)
    else: shutil.copy(os.path.join(SRC, name), dest)
    print(dest)
