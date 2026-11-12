"""Document ingestion: triggered by S3 ObjectCreated on uploads/{docId}/{name}.
Extracts text (PDF, DOCX, TXT, MD), chunks it, embeds each chunk with Titan
Text Embeddings v2 and stores the vectors in Aurora pgvector."""
import io
import re
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree

import boto3

import kb
from common import REGION, embed, log, metric, sql, sql_batch, vec_literal

s3 = boto3.client("s3", region_name=REGION)
CHUNK_CHARS = 1200
OVERLAP_CHARS = 200


# ------------------------------------------------------------------ extraction
def extract_pdf(data):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return [(i + 1, (p.extract_text() or "")) for i, p in enumerate(reader.pages)]


def extract_docx(data):
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(f"{{{ns['w']}}}p"):
        text = "".join(t.text or "" for t in p.iter(f"{{{ns['w']}}}t"))
        style = p.find("w:pPr/w:pStyle", ns)
        if style is not None and "Heading" in (style.get(f"{{{ns['w']}}}val") or "") and text:
            text = f"\n## {text}"
        paras.append(text)
    return [(None, "\n".join(paras))]


def extract(data, key, content_type):
    lower = key.lower()
    if lower.endswith(".pdf") or content_type == "application/pdf":
        return extract_pdf(data)
    if lower.endswith(".docx"):
        return extract_docx(data)
    return [(None, data.decode("utf-8", errors="replace"))]


# ------------------------------------------------------------------ chunking
def _split_long(text, size):
    parts, buf = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if len(buf) + len(sent) + 1 > size and buf:
            parts.append(buf)
            buf = ""
        while len(sent) > size:
            parts.append(sent[:size])
            sent = sent[size:]
        buf = f"{buf} {sent}".strip()
    if buf:
        parts.append(buf)
    return parts


def chunk_pages(pages):
    """Paragraph-aware chunking with overlap; keeps the page number of the
    chunk's first paragraph and prefixes the nearest section heading."""
    chunks = []
    for page, text in pages:
        text = re.sub(r"[\x7f\u2022\uf0b7]", "-", text.replace("\r", ""))        # bullet glyphs
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=#)", text) if p.strip()]
        if len(paras) <= 1:
            paras = [p.strip() for p in text.split("\n") if p.strip()]
        heading, buf = "", ""
        for para in paras:
            first = para.split("\n", 1)[0]
            new_heading = None
            if first.startswith("#") or (len(first) < 90 and first.isupper()):
                new_heading = first.lstrip("# ").strip()[:120]
            for piece in _split_long(para, CHUNK_CHARS):
                if len(buf) + len(piece) + 2 > CHUNK_CHARS and buf:
                    chunks.append((page, buf))
                    if new_heading:   # new section starts here: no overlap from the previous one
                        buf = ""
                    else:
                        tail = buf[-OVERLAP_CHARS:]
                        tail = tail[tail.find(" ") + 1:] if " " in tail else tail
                        buf = (f"[{heading}] " if heading else "") + tail
                buf = f"{buf}\n\n{piece}".strip()
                if new_heading:
                    heading, new_heading = new_heading, None
        if buf:
            chunks.append((page, buf))
    return [(p, c) for p, c in chunks if len(c) > 40]


# ------------------------------------------------------------------ handler
def process(bucket, key):
    m = re.match(r"^uploads/([0-9a-f]{16})/(.+)$", key)
    if not m:
        log.warning("ignoring unexpected key", extra={"key": key})
        return
    doc_id = m.group(1)
    rows = sql("SELECT id, name, content_type FROM documents WHERE id = :id", {"id": doc_id})
    if not rows:
        log.warning("no document row for upload; ignoring", extra={"docId": doc_id, "key": key})
        return
    doc = rows[0]
    t0 = time.time()
    sql("UPDATE documents SET status = 'processing', error = NULL WHERE id = :id", {"id": doc_id})
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        data = obj["Body"].read()
        pages = extract(data, key, doc["content_type"])
        chunks = chunk_pages(pages)
        if not chunks:
            raise ValueError("No extractable text found (scanned/image-only PDFs are not supported)")

        with ThreadPoolExecutor(max_workers=8) as pool:
            vectors = list(pool.map(lambda c: embed(c[1]), chunks))

        sql("DELETE FROM chunks WHERE doc_id = :id", {"id": doc_id})
        params = [{"doc": doc_id, "idx": i, "page": page, "content": text, "emb": vec_literal(vec)}
                  for i, ((page, text), vec) in enumerate(zip(chunks, vectors))]
        for start in range(0, len(params), 25):
            sql_batch("INSERT INTO chunks (doc_id, chunk_index, page, content, embedding) "
                      "VALUES (:doc, :idx, :page, :content, CAST(:emb AS vector))", params[start:start + 25])
        page_count = len(pages) if pages and pages[0][0] is not None else None
        sql("""UPDATE documents SET status = 'ready', chunk_count = :n, page_count = :pc, indexed_at = now(),
                      size_bytes = :size WHERE id = :id""",
            {"n": len(chunks), "pc": page_count, "size": len(data), "id": doc_id})
        ms = int((time.time() - t0) * 1000)
        metric("ingest", DocumentsIngested=1, ChunksIndexed=len(chunks), IngestDurationMs=ms)
        log.info("document indexed", extra={"docId": doc_id, "docName": doc["name"], "chunks": len(chunks),
                                            "pages": page_count, "bytes": len(data), "durationMs": ms})
    except Exception as e:
        sql("UPDATE documents SET status = 'failed', error = :err WHERE id = :id", {"err": str(e)[:500], "id": doc_id})
        metric("ingest", IngestFailures=1)
        log.exception("ingestion failed", extra={"docId": doc_id, "key": key, "error": str(e)})
        if isinstance(e, ValueError):
            return  # user error (e.g. empty doc): recorded on the document, don't retry/alarm
        raise


def handler(event, context):
    keys = []
    for rec in event.get("Records", []):
        bucket = rec["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        keys.append(key)
        process(bucket, key)          # custom pipeline: Aurora pgvector
    # managed pipeline: Bedrock Knowledge Base over the same S3 objects
    reason = event.get("reason") or (f"indexed {', '.join(k.rsplit('/', 1)[-1] for k in keys)}"[:200] if keys else "sync")
    if keys or event.get("action") == "kb-sync":
        try:
            kb.sync(reason)
        except Exception:  # noqa: BLE001 - KB sync must never fail the custom pipeline
            log.exception("kb sync failed")
