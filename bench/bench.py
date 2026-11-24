#!/usr/bin/env python3
"""PDF → Word quality benchmark.

Every PDF goes through the full round trip:  PDF → (engine) → DOCX → (LibreOffice) → PDF
and is compared with the original.

    python bench/bench.py corpus/ --engine app  --out results_app
    python bench/bench.py corpus/ --engine raw  --out results_raw
    python bench/bench.py --compare results_raw results_app

Metrics (all 0-100, higher = better):
  text    : how much of the original text survives in word order (difflib LCS ratio);
            for scanned documents '<name>.truth.txt' is used as the correct text
  visual  : page-by-page "ink" overlap (IoU, 2 px tolerance)
  pages   : page count consistency, penalty for extra blank pages
  TOTAL   : with text 0.5*text + 0.3*visual + 0.2*pages, otherwise 0.7*visual + 0.3*pages

Structures in the DOCX that cause trouble in Word are also counted
(free shapes, text boxes, fixed row heights).
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RENDER_W = 400  # comparison resolution (pixel width)


# ---------------------------------------------------------------- conversion

def convert(engine: str, pdf: Path, docx: Path) -> None:
    import logging
    logging.disable(logging.CRITICAL)
    if engine == "raw":
        from pdf2docx import Converter
        cv = Converter(str(pdf))
        cv.convert(str(docx))
        cv.close()
    elif engine == "app":
        from riffle import actions
        tmp_pdf = docx.parent / pdf.name
        shutil.copy2(pdf, tmp_pdf)
        out = actions.pdf_to_word([tmp_pdf], lambda m: None)[0]
        if out != docx:
            shutil.move(out, docx)
        tmp_pdf.unlink()
    else:
        raise ValueError(engine)


def docx_to_pdf(docx: Path, outdir: Path) -> Path:
    with tempfile.TemporaryDirectory() as prof:
        subprocess.run(["soffice", f"-env:UserInstallation={Path(prof).as_uri()}", "--headless",
                        "--convert-to", "pdf", "--outdir", str(outdir), str(docx)],
                       capture_output=True, timeout=600)
    return outdir / (docx.stem + ".pdf")


# ---------------------------------------------------------------- metrics

def words(doc) -> list[str]:
    text = " ".join(p.get_text() for p in doc)
    text = re.sub(r"-\s*\n\s*", "", text)          # line-end hyphenation
    return re.findall(r"\w+", text.lower())


def text_score(a: list[str], b: list[str]) -> float | None:
    if len(a) < 20:
        return None  # document without text (scanned / handwritten)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(bl.size for bl in sm.get_matching_blocks())
    return 100.0 * matched / max(len(a), 1)  # how much of the original is in the right order


def ink(page, width=RENDER_W) -> np.ndarray:
    z = width / page.rect.width
    pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, annots=False)
    im = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    return np.asarray(im) < 200


def iou(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        b = np.asarray(Image.fromarray(b.astype("uint8") * 255).resize(a.shape[::-1])) > 127
    if not a.any() and not b.any():
        return 1.0
    da = np.asarray(Image.fromarray(a.astype("uint8") * 255).filter(ImageFilter.MaxFilter(5))) > 0
    db = np.asarray(Image.fromarray(b.astype("uint8") * 255).filter(ImageFilter.MaxFilter(5))) > 0
    # both directions "has a counterpart": is a's ink within b's tolerance area and vice versa
    p = (a & db).sum() / max(a.sum(), 1)
    r = (b & da).sum() / max(b.sum(), 1)
    return 2 * p * r / max(p + r, 1e-9)


def blank(m: np.ndarray) -> bool:
    return m.mean() < 0.0015


def word_risks(x: str) -> list[str]:
    """Things that are invisible in LibreOffice but break the page layout in Word
    (learned from real Word comparisons)."""
    risks = []
    sects = re.findall(r"<w:sectPr(?: [^>]*)?>(.*?)</w:sectPr>", x, re.S)
    prev = None
    for i, s in enumerate(sects):
        t = re.search(r'<w:type w:val="(\w+)"', s)
        t = t.group(1) if t else "nextPage"
        mar = re.search(r"<w:pgMar [^>]*/>", s)
        mar = re.sub(r'w:(header|footer|gutter)="\d+"', "", mar.group(0)) if mar else ""
        size = re.search(r"<w:pgSz [^>]*/>", s)
        size = size.group(0) if size else ""
        # Word: a 'continuous' section break with changing margins/page size = PAGE BREAK
        if prev and t == "continuous" and (mar != prev[0] or size != prev[1]):
            risks.append(f"section {i + 1}: margins/page size change at a continuous break (Word inserts a page break)")
        # LibreOffice treats a 'next column' section break as a page break
        if t == "nextColumn":
            risks.append(f"section {i + 1}: 'next column' section break (LibreOffice treats it as a page break)")
        prev = (mar, size)
    if re.search(r'w:(?:w|sz|space|val)="-?\d+\.\d+"', x):
        risks.append("decimal measure value (Word may report 'unreadable content')")
    if re.search(r'w:(?:color|fill)="#', x):
        risks.append("colour value with a '#' prefix")
    if "<wps:wsp>" in x and "mc:AlternateContent" not in x:
        risks.append("text box not inside mc:AlternateContent")
    return risks


def docx_flags(docx: Path) -> dict:
    x = zipfile.ZipFile(docx).read("word/document.xml").decode("utf-8", "ignore")
    return {
        "word_risk": len(word_risks(x)),
        "free_shapes": x.count("<wp:anchor"),
        "text_boxes": x.count("<w:txbxContent"),
        "images": x.count("<a:blip "),
        "fixed_rows": x.count('w:hRule="exact"'),
        "tables": x.count("<w:tbl>"),
    }


# ---------------------------------------------------------------- single file

def run_one(args) -> dict:
    engine, pdf, outdir = args
    pdf, outdir = Path(pdf), Path(outdir)
    work = outdir / pdf.stem
    work.mkdir(parents=True, exist_ok=True)
    docx = work / (pdf.stem + ".docx")
    res = {"file": pdf.name}
    t0 = time.time()
    try:
        convert(engine, pdf, docx)
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    res["time_s"] = round(time.time() - t0, 1)
    rt_pdf = docx_to_pdf(docx, work)
    if not rt_pdf.exists():
        res["error"] = "LibreOffice could not open the DOCX"
        return res

    a, b = pymupdf.open(pdf), pymupdf.open(rt_pdf)
    res["pages_orig"], res["pages_new"] = len(a), len(b)
    res.update(docx_flags(docx))

    # Scanned documents have no text in the original: if '<name>.truth.txt' exists, that is the correct text
    truth = pdf.with_name(pdf.stem + ".truth.txt")
    ref = re.findall(r"\w+", re.sub(r"-\s*\n\s*", "", truth.read_text(encoding="utf-8")).lower()) \
        if truth.exists() else words(a)
    ts = text_score(ref, words(b))
    res["text"] = None if ts is None else round(ts, 1)

    ia = [ink(p) for p in a]
    ib = [ink(p) for p in b]
    extra_blank = max(0, sum(map(blank, ib)) - sum(map(blank, ia)))
    res["extra_blank_pages"] = extra_blank
    per_page = [round(100 * iou(ia[i], ib[i]), 1) if i < len(ib) else 0.0 for i in range(len(ia))]
    res["page_visual"] = per_page
    # so page drift alone isn't penalised: each original page takes the best matching page within ±1
    shifted = []
    for i in range(len(ia)):
        cands = [iou(ia[i], ib[j]) for j in (i - 1, i, i + 1) if 0 <= j < len(ib)]
        shifted.append(100 * max(cands) if cands else 0)
    res["visual"] = round(float(np.mean(shifted)), 1)

    n = len(a)
    page_pen = abs(len(b) - n) / n + extra_blank / n
    res["pages"] = round(100 * max(0.0, 1 - page_pen), 1)

    if res["text"] is None:
        res["TOTAL"] = round(0.7 * res["visual"] + 0.3 * res["pages"], 1)
    else:
        res["TOTAL"] = round(0.5 * res["text"] + 0.3 * res["visual"] + 0.2 * res["pages"], 1)
    return res


# ---------------------------------------------------------------- report

COLS = ["file", "TOTAL", "text", "visual", "pages", "pages_orig", "pages_new",
        "extra_blank_pages", "word_risk", "free_shapes", "text_boxes", "fixed_rows", "time_s"]


def table(rows: list[dict]) -> str:
    lines = ["| " + " | ".join(COLS) + " |", "|" + "---|" * len(COLS)]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['file']} | ERROR: {r['error']} |")
            continue
        lines.append("| " + " | ".join("–" if r.get(c) is None else str(r.get(c)) for c in COLS) + " |")
    ok = [r for r in rows if "error" not in r]
    if ok:
        lines.append(f"| **AVERAGE** | **{np.mean([r['TOTAL'] for r in ok]):.1f}** |")
    return "\n".join(lines)


def compare(a_dir: Path, b_dir: Path) -> str:
    A = {r["file"]: r for r in json.loads((a_dir / "results.json").read_text())}
    B = {r["file"]: r for r in json.loads((b_dir / "results.json").read_text())}
    out = [f"| dosya | {a_dir.name} | {b_dir.name} | diff |", "|---|---|---|---|"]
    for k in A:
        ta, tb = A[k].get("TOTAL"), B.get(k, {}).get("TOTAL")
        d = "" if ta is None or tb is None else f"{tb - ta:+.1f}"
        out.append(f"| {k} | {ta} | {tb} | {d} |")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", nargs="?")
    ap.add_argument("--engine", default="app", choices=["app", "raw"])
    ap.add_argument("--out", default="results")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--compare", nargs=2)
    a = ap.parse_args()
    if a.compare:
        print(compare(Path(a.compare[0]), Path(a.compare[1])))
        return
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(Path(a.corpus).glob("*.pdf"))
    with ProcessPoolExecutor(a.jobs) as ex:
        rows = list(ex.map(run_one, [(a.engine, str(p), str(out)) for p in pdfs]))
    (out / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=lambda o: o.item()))
    md = table(rows)
    details = []
    for p in pdfs:
        d = out / p.stem / (p.stem + ".docx")
        if d.exists():
            for r in word_risks(zipfile.ZipFile(d).read("word/document.xml").decode("utf-8", "ignore")):
                details.append(f"- {p.name}: {r}")
    if details:
        md += "\n\n**Word risks:**\n" + "\n".join(details)
    (out / "results.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
