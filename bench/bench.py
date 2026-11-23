#!/usr/bin/env python3
"""PDF → Word kalite ölçümü.

Her PDF için tam tur yapılır:  PDF → (motor) → DOCX → (LibreOffice) → PDF
ve orijinal ile karşılaştırılır.

    python bench/bench.py corpus/ --engine app  --out sonuc_app
    python bench/bench.py corpus/ --engine raw  --out sonuc_raw
    python bench/bench.py --compare sonuc_raw sonuc_app

Metrikler (hepsi 0-100, yüksek = iyi):
  metin   : orijinal metnin kelime sırasıyla ne kadarı korunmuş (difflib LCS oranı);
            taranmış belgelerde '<ad>.truth.txt' doğru metin olarak kullanılır
  görsel  : sayfa sayfa "mürekkep" örtüşmesi (IoU, 2 px tolerans)
  sayfa   : sayfa sayısı tutarlılığı, fazladan boş sayfa cezası
  TOPLAM  : metin varsa 0.5*metin + 0.3*görsel + 0.2*sayfa, yoksa 0.7*görsel + 0.3*sayfa

Ayrıca DOCX içinde Word'de sorun çıkaran yapılar sayılır
(serbest şekil, metin kutusu, sabit satır yüksekliği).
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

RENDER_W = 400  # karşılaştırma çözünürlüğü (piksel genişlik)


# ---------------------------------------------------------------- dönüşüm

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


# ---------------------------------------------------------------- metrikler

def words(doc) -> list[str]:
    text = " ".join(p.get_text() for p in doc)
    text = re.sub(r"-\s*\n\s*", "", text)          # satır sonu tirelemesi
    return re.findall(r"\w+", text.lower())


def text_score(a: list[str], b: list[str]) -> float | None:
    if len(a) < 20:
        return None  # metinsiz (taranmış / el yazısı) belge
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(bl.size for bl in sm.get_matching_blocks())
    return 100.0 * matched / max(len(a), 1)  # orijinalin ne kadarı doğru sırada


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
    # her iki yönde "karşılığı var mı": a'nın mürekkebi b'nin toleranslı alanında mı ve tersi
    p = (a & db).sum() / max(a.sum(), 1)
    r = (b & da).sum() / max(b.sum(), 1)
    return 2 * p * r / max(p + r, 1e-9)


def blank(m: np.ndarray) -> bool:
    return m.mean() < 0.0015


def word_risks(x: str) -> list[str]:
    """LibreOffice'te görünmeyen ama Word'de sayfa düzenini bozan durumlar
    (gerçek Word karşılaştırmalarından öğrenildi)."""
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
        # Word: kenar boşluğu/sayfa boyutu değişen 'sürekli' bölüm sonu = SAYFA SONU
        if prev and t == "continuous" and (mar != prev[0] or size != prev[1]):
            risks.append(f"bölüm {i + 1}: sürekli geçişte kenar boşluğu/sayfa boyutu değişiyor (Word sayfa sonu koyar)")
        # LibreOffice 'sonraki sütun' bölüm sonunu sayfa sonu sayar
        if t == "nextColumn":
            risks.append(f"bölüm {i + 1}: 'sonraki sütun' bölüm sonu (LibreOffice sayfa sonu sayar)")
        prev = (mar, size)
    if re.search(r'w:(?:w|sz|space|val)="-?\d+\.\d+"', x):
        risks.append("ondalıklı ölçü değeri (Word 'okunamayan içerik' uyarısı verebilir)")
    if re.search(r'w:(?:color|fill)="#', x):
        risks.append("'#' önekli renk değeri")
    if "<wps:wsp>" in x and "mc:AlternateContent" not in x:
        risks.append("metin kutusu mc:AlternateContent içinde değil")
    return risks


def docx_flags(docx: Path) -> dict:
    x = zipfile.ZipFile(docx).read("word/document.xml").decode("utf-8", "ignore")
    return {
        "word_risk": len(word_risks(x)),
        "serbest_sekil": x.count("<wp:anchor"),
        "metin_kutusu": x.count("<w:txbxContent"),
        "gorsel": x.count("<a:blip "),
        "sabit_satir": x.count('w:hRule="exact"'),
        "tablo": x.count("<w:tbl>"),
    }


# ---------------------------------------------------------------- tek dosya

def run_one(args) -> dict:
    engine, pdf, outdir = args
    pdf, outdir = Path(pdf), Path(outdir)
    work = outdir / pdf.stem
    work.mkdir(parents=True, exist_ok=True)
    docx = work / (pdf.stem + ".docx")
    res = {"dosya": pdf.name}
    t0 = time.time()
    try:
        convert(engine, pdf, docx)
    except Exception as e:
        res["hata"] = f"{type(e).__name__}: {e}"
        return res
    res["sure_sn"] = round(time.time() - t0, 1)
    rt_pdf = docx_to_pdf(docx, work)
    if not rt_pdf.exists():
        res["hata"] = "LibreOffice DOCX'i açamadı"
        return res

    a, b = pymupdf.open(pdf), pymupdf.open(rt_pdf)
    res["sayfa_orj"], res["sayfa_yeni"] = len(a), len(b)
    res.update(docx_flags(docx))

    # Taranmış belgelerde orijinalde metin yok: '<ad>.truth.txt' varsa doğru metin odur
    truth = pdf.with_name(pdf.stem + ".truth.txt")
    ref = re.findall(r"\w+", re.sub(r"-\s*\n\s*", "", truth.read_text(encoding="utf-8")).lower()) \
        if truth.exists() else words(a)
    ts = text_score(ref, words(b))
    res["metin"] = None if ts is None else round(ts, 1)

    ia = [ink(p) for p in a]
    ib = [ink(p) for p in b]
    extra_blank = max(0, sum(map(blank, ib)) - sum(map(blank, ia)))
    res["fazla_bos_sayfa"] = extra_blank
    per_page = [round(100 * iou(ia[i], ib[i]), 1) if i < len(ib) else 0.0 for i in range(len(ia))]
    res["sayfa_gorsel"] = per_page
    # sayfa kaymasını tek başına cezalandırmamak için: her orijinal sayfa en iyi eşleşen ±1 sayfa
    shifted = []
    for i in range(len(ia)):
        cands = [iou(ia[i], ib[j]) for j in (i - 1, i, i + 1) if 0 <= j < len(ib)]
        shifted.append(100 * max(cands) if cands else 0)
    res["gorsel"] = round(float(np.mean(shifted)), 1)

    n = len(a)
    page_pen = abs(len(b) - n) / n + extra_blank / n
    res["sayfa"] = round(100 * max(0.0, 1 - page_pen), 1)

    if res["metin"] is None:
        res["TOPLAM"] = round(0.7 * res["gorsel"] + 0.3 * res["sayfa"], 1)
    else:
        res["TOPLAM"] = round(0.5 * res["metin"] + 0.3 * res["gorsel"] + 0.2 * res["sayfa"], 1)
    return res


# ---------------------------------------------------------------- rapor

COLS = ["dosya", "TOPLAM", "metin", "gorsel", "sayfa", "sayfa_orj", "sayfa_yeni",
        "fazla_bos_sayfa", "word_risk", "serbest_sekil", "metin_kutusu", "sabit_satir", "sure_sn"]


def table(rows: list[dict]) -> str:
    lines = ["| " + " | ".join(COLS) + " |", "|" + "---|" * len(COLS)]
    for r in rows:
        if "hata" in r:
            lines.append(f"| {r['dosya']} | HATA: {r['hata']} |")
            continue
        lines.append("| " + " | ".join("–" if r.get(c) is None else str(r.get(c)) for c in COLS) + " |")
    ok = [r for r in rows if "hata" not in r]
    if ok:
        lines.append(f"| **ORTALAMA** | **{np.mean([r['TOPLAM'] for r in ok]):.1f}** |")
    return "\n".join(lines)


def compare(a_dir: Path, b_dir: Path) -> str:
    A = {r["dosya"]: r for r in json.loads((a_dir / "sonuc.json").read_text())}
    B = {r["dosya"]: r for r in json.loads((b_dir / "sonuc.json").read_text())}
    out = [f"| dosya | {a_dir.name} | {b_dir.name} | fark |", "|---|---|---|---|"]
    for k in A:
        ta, tb = A[k].get("TOPLAM"), B.get(k, {}).get("TOPLAM")
        d = "" if ta is None or tb is None else f"{tb - ta:+.1f}"
        out.append(f"| {k} | {ta} | {tb} | {d} |")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", nargs="?")
    ap.add_argument("--engine", default="app", choices=["app", "raw"])
    ap.add_argument("--out", default="sonuc")
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
    (out / "sonuc.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=lambda o: o.item()))
    md = table(rows)
    details = []
    for p in pdfs:
        d = out / p.stem / (p.stem + ".docx")
        if d.exists():
            for r in word_risks(zipfile.ZipFile(d).read("word/document.xml").decode("utf-8", "ignore")):
                details.append(f"- {p.name}: {r}")
    if details:
        md += "\n\n**Word riskleri:**\n" + "\n".join(details)
    (out / "sonuc.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
