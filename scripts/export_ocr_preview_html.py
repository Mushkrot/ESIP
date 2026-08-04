#!/usr/bin/env python3
"""
Build a local HTML preview: cohort images + production ocr_image() output split into
  (1) [IMAGE DESCRIPTION: …] line (first line)
  (2) extracted text body

Writes under scripts/ocr_preview_export/ (images + index.html). Open index.html in a browser.

Costs N Gemini OCR calls (same CIS path as production sync).

Usage:
  python3 scripts/export_ocr_preview_html.py [--limit 5] [--out scripts/ocr_preview_export]

Does NOT write BigQuery or touch done / failure JSON.
"""

from __future__ import annotations

import argparse
import base64
import html
import importlib.util
import logging
import sys
from pathlib import Path

sys.path.insert(0, "backend")

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

import requests

from app.services.bigquery import _get_client
from app.services.cost_tracker import flush_bq_events
from app.services.gemini import ocr_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("export_ocr_preview_html")

MAX_IMAGE_BYTES = 20 * 1024 * 1024
SUPPORTED_MIME = {
    "image/jpeg", "image/jpg", "image/png", "image/gif",
    "image/webp", "image/bmp", "image/tiff",
}


def _load_probe():
    path = Path(__file__).resolve().parent / "probe_ocr_no_description.py"
    spec = importlib.util.spec_from_file_location("probe_ocr_no_description", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def split_description_and_body(full: str) -> tuple[str, str]:
    """First line = [IMAGE DESCRIPTION: …]; rest = body (after optional blank line)."""
    full = (full or "").strip()
    if not full.startswith("[IMAGE DESCRIPTION:"):
        return "", full
    lines = full.split("\n", 1)
    desc_line = lines[0].strip()
    body = (lines[1].lstrip("\n") if len(lines) > 1 else "").strip()
    return desc_line, body


def mime_to_data_url(mime: str, raw: bytes) -> str:
    m = mime.split(";")[0].strip().lower()
    if m == "image/jpg":
        m = "image/jpeg"
    if m not in SUPPORTED_MIME:
        m = "image/jpeg"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{m};base64,{b64}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5, help="Number of images (default 5, max 12)")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("scripts/ocr_preview_export"),
        help="Output directory (created if missing)",
    )
    args = ap.parse_args()
    n = max(1, min(int(args.limit), 12))
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    probe = _load_probe()
    client = _get_client()
    rows = probe.fetch_sample(client, n)
    if not rows:
        logger.error("Cohort empty — nothing to export.")
        sys.exit(1)

    logger.info("Exporting %d image(s) to %s (N OCR calls).", len(rows), out.resolve())

    cards: list[str] = []
    for i, row in enumerate(rows, 1):
        tid = row["ticket_id"]
        url = row["content_url"]
        try:
            r = requests.get(url, timeout=45)
            if r.status_code != 200 or len(r.content) > MAX_IMAGE_BYTES:
                logger.warning("Skip ticket %s: HTTP %s or size", tid, r.status_code)
                continue
        except Exception as exc:
            logger.warning("Skip ticket %s: %s", tid, exc)
            continue

        ct = r.headers.get("Content-Type", row["content_type"]).split(";")[0].strip()
        if ct not in SUPPORTED_MIME:
            ct = "image/jpeg"
        ext = ".png" if "png" in ct else ".jpg" if "jpeg" in ct or "jpg" in ct else ".webp" if "webp" in ct else ".bin"
        img_path = out / f"img_{i:02d}_ticket_{tid}{ext}"
        img_path.write_bytes(r.content)

        new_text, err = ocr_image(r.content, ct)
        if err or not new_text:
            logger.warning("OCR failed ticket %s: %s", tid, err)
            desc, body = "", f"(OCR error: {html.escape(err or 'empty')})"
        else:
            desc, body = split_description_and_body(new_text)

        data_url = mime_to_data_url(ct, r.content)
        cards.append(
            f"""
<section class="card">
  <h2>#{i} — ticket <code>{html.escape(str(tid))}</code></h2>
  <p class="meta">Saved file: <code>{html.escape(img_path.name)}</code></p>
  <div class="row">
    <div class="col imgcol">
      <p class="label">Image</p>
      <img src="{data_url}" alt="ticket {tid}" />
    </div>
    <div class="col textcol">
      <p class="label">Description line (PART 1)</p>
      <pre class="box desc">{html.escape(desc or "(none)")}</pre>
      <p class="label">Extracted text (PART 2)</p>
      <pre class="box body">{html.escape(body)}</pre>
      <p class="hint">Full OCR length: {len(new_text or "")} chars · DB old_len was {row["old_len"]}</p>
    </div>
  </div>
</section>
"""
        )
        logger.info("Done %d/%d ticket=%s", i, len(rows), tid)

    css = """
body { font-family: system-ui, sans-serif; margin: 24px; background: #f4f4f5; color: #18181b; }
h1 { font-size: 1.25rem; }
.card { background: #fff; border: 1px solid #SYNTHETIC-POSTAL; border-radius: 12px; padding: 20px; margin-bottom: 28px; box-shadow: 0 1px 2px rgb(0 0 0 / 0.05); }
.meta { color: #71717a; font-size: 0.85rem; }
.row { display: flex; flex-wrap: wrap; gap: 20px; margin-top: 12px; align-items: flex-start; }
.col { min-width: 280px; }
.imgcol { flex: 0 0 auto; }
.imgcol img { max-width: min(520px, 100%); height: auto; border: 1px solid #SYNTHETIC-POSTAL; border-radius: 8px; }
.textcol { flex: 1 1 400px; min-width: 0; }
.label { font-weight: 600; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.04em; color: #52525b; margin: 12px 0 6px; }
.box { margin: 0; padding: 12px; background: #fafafa; border: 1px solid #SYNTHETIC-POSTAL; border-radius: 8px; white-space: pre-wrap; word-break: break-word; font-size: 0.8rem; line-height: 1.45; max-height: 420px; overflow: auto; }
.box.desc { max-height: 120px; background: #ecfdf5; border-color: #a7f3d0; }
.hint { font-size: 0.75rem; color: #71717a; margin-top: 8px; }
"""

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>OCR preview (production ocr_image)</title>
  <style>{css}</style>
</head>
<body>
  <h1>OCR preview — production <code>ocr_image()</code> (description first, single call)</h1>
  <p>Generated locally. Images embedded as data URLs. PART 2 may be truncated by shared max_output_tokens=2048.</p>
  {"".join(cards)}
</body>
</html>
"""

    index = out / "index.html"
    index.write_text(html_doc, encoding="utf-8")
    logger.info("Wrote %s — open this file in your browser.", index.resolve())
    flush_bq_events(timeout=15.0)


if __name__ == "__main__":
    main()
