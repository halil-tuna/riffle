"""Akan belgeler için PDF analizi ve DOCX bölüm düzeltmeleri.

1) Figür kurtarma (PDF ön işleme):
   pdf2docx vektör çizimleri (grafikler) tamamen atlıyor, faks (CCITT) / JBIG2 /
   maske resimlerini de okuyamıyor. Bu bölgeleri orijinalden yüksek çözünürlükte
   resme çevirip aynı yere koyuyoruz; pdf2docx sıradan bir resim olarak aktarıyor.

2) Sayfa akışı (DOCX son işleme):
   pdf2docx her PDF sayfasını 'yeni sayfa' bölüm sonuyla bitiriyor. Word'de birkaç
   satır taşınca sonraki sayfa neredeyse boş kalıyor. Orijinalde DOLU biten
   sayfalardan sonraki geçişler 'sürekli' yapılır (metin akar), tekrar eden üst/alt
   bilgiler (dergi adı, sayfa no) silinir. Yarıda biten sayfalar (kasıtlı sayfa
   sonu) korunur.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

try:
    import pymupdf
except ImportError:  # pragma: no cover
    try:
        import fitz as pymupdf  # type: ignore
    except ImportError:          # install.py kütüphaneler kurulmadan da yükleyebilsin
        pymupdf = None  # type: ignore

FIG_DPI = 200
BAND = 0.075  # sayfanın üst/alt %7,5'i: üst bilgi / alt bilgi bandı


# =========================================================== 1) figür kurtarma

def _shape_score(path: dict) -> int:
    """Grafik gibi mi? Eğri ve çapraz çizgi sayısı (tablo kenarlığı/dolgu 0 alır)."""
    score = 0
    for it in path.get("items", []):
        if it[0] == "c":
            score += 1
        elif it[0] == "l":
            p1, p2 = it[1], it[2]
            if abs(p1.x - p2.x) > 1 and abs(p1.y - p2.y) > 1:
                score += 1
        elif it[0] == "qu":
            score += 1
    return score


def _cluster(rects: list, gap: float = 6.0) -> list:
    rects = [pymupdf.Rect(r) for r in rects]
    merged = True
    while merged:
        merged = False
        out: list = []
        for r in rects:
            for i, o in enumerate(out):
                if (r + (-gap, -gap, gap, gap)).intersects(o):
                    out[i] = o | r
                    merged = True
                    break
            else:
                out.append(r)
        rects = out
    return rects


_BAD_IMG = ("CCITTFaxDecode", "JBIG2Decode", "JPXDecode")


def figure_regions(page) -> list:
    """Bu sayfada pdf2docx'in kaçıracağı figür bölgeleri."""
    regions = []
    # a) Grafik benzeri vektör çizim kümeleri
    drawings = page.get_drawings()
    if drawings:
        groups: list[tuple] = []  # (rect, score, count)
        for cl in _cluster([d["rect"] for d in drawings if d["rect"].width + d["rect"].height > 0]):
            inside = [d for d in drawings if pymupdf.Rect(d["rect"]) in cl + (-1, -1, 1, 1)]
            score = sum(_shape_score(d) for d in inside)
            groups.append((cl, score, len(inside)))
        for rect, score, count in groups:
            if score >= 8 and rect.width >= 40 and rect.height >= 30:
                regions.append(rect)
    # b) pdf2docx'in okuyamadığı resimler
    for im in page.get_images(full=True):
        xref, smask, cs, filt = im[0], im[1], im[5], im[8]
        if filt in _BAD_IMG or cs == "" or im[4] == 1:
            try:
                for r in page.get_image_rects(xref):
                    if r.width > 15 and r.height > 15:
                        regions.append(pymupdf.Rect(r))
            except Exception:
                pass
    if not regions:
        return []
    regions = _cluster(regions, gap=4)
    # Figürün içindeki / hemen dibindeki kısa etiketleri (eksen yazıları) dahil et,
    # 'Fig.' / 'Table' / 'Şekil' ile başlayan başlıkları DIŞARIDA bırak.
    lines = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if t:
                lines.append((pymupdf.Rect(ln["bbox"]), t))
    caption = re.compile(r"^(fig(ure)?|table|tablo|şekil|sekil|resim|grafik)\b", re.I)
    out = []
    for r in regions:
        grown = pymupdf.Rect(r)
        for lr, t in lines:
            if caption.match(t) or len(t) > 60:
                continue
            if lr.intersects(r + (-14, -14, 14, 14)):
                grown |= lr
        grown &= page.rect
        # sayfanın yarısından büyük 'figür' muhtemelen yanlış alarm (sayfa çerçevesi vb.)
        if grown.is_empty or grown.get_area() > 0.6 * page.rect.get_area():
            continue
        # içi yazıyla dolu bölge figür değil, yuvarlak köşeli metin kutusu / tablo
        text_area = sum((lr & grown).get_area() for lr, t in lines if lr.intersects(grown))
        if text_area > 0.25 * grown.get_area():
            continue
        out.append(grown)
    return out


def rescue_figures(doc) -> int:
    """Figür bölgelerini yerinde resme çevirir. Kurtarılan figür sayısını döndürür."""
    count = 0
    for page in doc:
        regions = figure_regions(page)
        if not regions:
            continue
        shots = []
        for r in regions:
            pix = page.get_pixmap(dpi=FIG_DPI, clip=r, alpha=False, annots=False)
            shots.append((r, pix.tobytes("png")))
        for r, _ in shots:
            page.add_redact_annot(r, fill=(1, 1, 1))
        # bölgedeki resimleri, çizimleri ve yazıyı kaldır
        page.apply_redactions(images=1, graphics=2, text=0)
        for r, png in shots:
            page.insert_image(r, stream=png)
            count += 1
    return count


# =========================================================== 2) sayfa analizi

@dataclass
class PageInfo:
    full: list[bool] = field(default_factory=list)       # sayfa dolu mu bitiyor
    headers: set[str] = field(default_factory=set)        # tekrar eden üst/alt bilgi (normalize)
    page_numbers: set[str] = field(default_factory=set)   # bant içindeki salt sayı satırları
    rules: list = field(default_factory=list)             # paragraf kenarlığı olacak yatay çizgiler


# =========================================================== yatay ayırıcı çizgiler

@dataclass
class Rule:
    text: str            # çizginin yapışacağı satırın metni (normalize)
    side: str            # "bottom": çizgi satırın altında · "top": üstünde
    color: str           # RRGGBB
    width: float         # pt
    gap: float           # metinle çizgi arası, pt


def _rgb(c) -> str:
    c = c or (0, 0, 0)
    return "".join(f"{max(0, min(255, round(v * 255))):02X}" for v in c[:3])


def find_rules(page) -> list[Rule]:
    """Başlık altı / bölüm ayırıcı gibi tek başına duran yatay çizgiler.

    pdf2docx yalnızca tablo kenarlığı ve alt çizgiyi tanıyor; sayfa boyu ayırıcı çizgiler
    Word'de kayboluyordu. Bunlar en yakın satırın paragraf kenarlığı olarak geri konur.
    Tablo çizgileri (dikey çizgiyle kesişen ya da tablo içindeki) ve kelime altı çizgileri atlanır.
    """
    W = page.rect.width
    try:
        drawings = page.get_drawings()
    except Exception:
        return []
    horiz, vert = [], []
    for d in drawings:
        rr = pymupdf.Rect(d["rect"])
        if rr.height <= 2.5 and rr.width >= max(0.25 * W, 100):
            horiz.append((rr, d))
        elif rr.width <= 2.5 and rr.height >= 6:
            vert.append(rr)
    if not horiz:
        return []
    try:
        tables = [pymupdf.Rect(t.bbox) for t in page.find_tables().tables]
    except Exception:
        tables = []
    lines = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if t:
                lines.append((pymupdf.Rect(ln["bbox"]), t))
    out, seen = [], []
    for rr, d in sorted(horiz, key=lambda x: x[0].y0):
        if any(rr.intersects(t + (-3, -3, 3, 3)) for t in tables):
            continue
        if any(rr.x0 - 3 <= v.x0 <= rr.x1 + 3 and v.y0 - 3 <= rr.y0 <= v.y1 + 3 for v in vert):
            continue                                    # dikey çizgiyle kesişiyor: tablo/çerçeve
        if any(abs(rr.y0 - s.y0) < 1.5 and abs(rr.x0 - s.x0) < 3 for s in seen):
            continue                                    # aynı çizgi iki kez (kontur + dolgu)
        overlap = [(lr, t) for lr, t in lines if min(lr.x1, rr.x1) - max(lr.x0, rr.x0) > 0]
        above = [(rr.y0 - lr.y1, lr, t) for lr, t in overlap if -1 <= rr.y0 - lr.y1 <= 18]
        below = [(lr.y0 - rr.y1, lr, t) for lr, t in overlap if -1 <= lr.y0 - rr.y1 <= 18]
        if above and below and min(x[0] for x in above) < 6 and min(x[0] for x in below) < 6:
            continue                                    # kesir çizgisi / sıkışık tablo satırı
        if above:
            gap, lr, t = min(above, key=lambda x: x[0])
            if gap < 3 and lr.width > 0.85 * rr.width:
                continue                                # kelime altı çizgisi: pdf2docx zaten yapıyor
            side = "bottom"
        elif below:
            gap, lr, t = min(below, key=lambda x: x[0])
            side = "top"
        else:
            continue
        if len(_norm_text(t)) < 3 or (lr.width < 0.15 * rr.width and rr.width < 0.5 * W):
            continue                                    # denklemdeki birim/sembol satırı, ayırıcı değil
        width = d.get("width") or rr.height or 0.75
        color = d.get("color") if d.get("color") is not None else d.get("fill")
        if color is not None and min(color[:3]) > 0.94:
            continue                                    # beyaz çizgi: görünmez
        out.append(Rule(_norm_text(t), side, _rgb(color), max(0.25, min(float(width), 6.0)), max(0.0, gap)))
        seen.append(rr)
    return out


_LIG = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl"}


def _norm_text(t: str) -> str:
    """Eşleştirme anahtarı: yalnızca harf/rakam (bağlı harf açılmış, tire/boşluk/sembol atılmış).
    Word tarafında satır sonu tireleri birleşmiş, μ kurtarılmış olabilir; ikisi de aynı anahtara iner."""
    t = "".join(_LIG.get(c, c) for c in t)
    return re.sub(r"[\W_]+", "", t).lower()


def is_two_column(doc, sample: int = 12) -> bool:
    """Akademik makale gibi iki sütunlu mu? Sayfaların çoğunda iki yanda da dolu sütun olmalı
    ve ortayı kesen satırlar az olmalı."""
    pages = [doc[i] for i in range(min(len(doc), sample))]
    votes = total = 0
    for page in pages:
        W = page.rect.width
        mid = W / 2
        left = right = cross = 0
        try:
            tables = [pymupdf.Rect(t.bbox) for t in page.find_tables().tables]
        except Exception:
            tables = []
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                if not "".join(s["text"] for s in ln["spans"]).strip():
                    continue
                r = pymupdf.Rect(ln["bbox"])
                if any(r.intersects(t) for t in tables):
                    continue                            # tablo hücreleri sütun sayılmaz
                x0, x1 = r.x0, r.x1
                if x1 - x0 > 0.55 * W:
                    cross += 1
                elif x1 - x0 < 0.28 * W:
                    continue                            # kısa satırlar (paragraf sonu, etiket) oy vermez
                elif x1 <= mid + 6:
                    left += 1
                elif x0 >= mid - 6:
                    right += 1
        n = left + right + cross
        if n < 15:
            continue
        total += 1
        if left >= 10 and right >= 10 and cross / n < 0.3:
            votes += 1
    return total > 0 and votes / total >= 0.5


def _norm(t: str) -> str:
    # boşluklar tamamen atılır: boşluksuz PDF'lerde Word metni 'JournalofMaterials' olabiliyor
    return re.sub(r"\s+", "", re.sub(r"\d+", "#", t)).lower()


def analyze_pages(doc) -> PageInfo:
    info = PageInfo()
    n = len(doc)
    per_page = []
    band_norms: Counter = Counter()
    for page in doc:
        H = page.rect.height
        lines = []
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = "".join(s["text"] for s in ln["spans"]).strip()
                if t:
                    lines.append((pymupdf.Rect(ln["bbox"]), t))
        per_page.append(lines)
        seen = set()
        for r, t in lines:
            if r.y1 < BAND * H or r.y0 > (1 - BAND) * H:
                k = _norm(t)
                if k not in seen:
                    band_norms[k] += 1
                    seen.add(k)
                if t.isdigit() and len(t) <= 4:
                    info.page_numbers.add(t)
    info.headers = {k for k, c in band_norms.items() if c >= max(2, 0.3 * n) and len(k) >= 4}

    bottoms, heights = [], []
    for page, lines in zip(doc, per_page):
        H = page.rect.height
        body = [r for r, t in lines
                if not ((r.y1 < BAND * H or r.y0 > (1 - BAND) * H)
                        and (_norm(t) in info.headers or t.isdigit()))]
        heights += [r.height for r in body]
        bottom = max((r.y1 for r in body), default=0)
        for im in page.get_image_info():
            bottom = max(bottom, im["bbox"][3])
        bottoms.append(bottom)
    if not bottoms:
        return info
    line_h = sorted(heights)[len(heights) // 2] if heights else 12
    ref = sorted(bottoms)[int(0.75 * (len(bottoms) - 1))]  # tipik dolu sayfanın alt sınırı (dipnota duyarsız)
    info.full = [b >= max(ref - 2.5 * line_h, 0.75 * page.rect.height)
                 for b, page in zip(bottoms, doc)]
    return info


# =========================================================== 3) DOCX bölümleri

_SECT_P = re.compile(r'<w:p><w:pPr><w:sectPr>(.*?)</w:sectPr></w:pPr></w:p>', re.S)


def _stype(sect: str) -> str:
    m = re.search(r'<w:type w:val="(\w+)"', sect)
    return m.group(1) if m else "nextPage"


def _cols(sect: str) -> str:
    m = re.search(r'<w:cols w:num="(\d+)"', sect)
    return m.group(1) if m else "1"


def _unify_page_setup(sections):
    """Yalnızca 'sürekli' geçişle birbirine bağlı bölüm zincirlerini eşitler;
    yeni sayfa ile başlayan bölüm yeni bir zincir başlatır (kendi boşluğunu korur)."""
    out, chain = [], []
    for c, s in sections:
        if chain and _stype(s) in ("nextPage", "oddPage", "evenPage"):
            out += _unify_chain(chain)
            chain = []
        chain.append([c, s])
    return out + _unify_chain(chain)


def _unify_chain(sections):
    margins = []
    for _, s in sections:
        m = re.search(r'<w:pgMar ([^>]*)/>', s)
        if m:
            margins.append({k: int(v) for k, v in re.findall(r'w:(\w+)="(-?\d+)"', m.group(1))})
    if len(margins) < 2:
        return sections
    side = round(sum(m.get("left", 0) + m.get("right", 0) for m in margins) / len(margins) / 2)
    top = min(m.get("top", 720) for m in margins)
    bottom = max(300, min(m.get("bottom", 720) for m in margins))
    pg = (f'<w:pgMar w:top="{top}" w:right="{side}" w:bottom="{bottom}" w:left="{side}" '
          'w:header="720" w:footer="720" w:gutter="0"/>')
    out = []
    for c, s in sections:
        s = re.sub(r'<w:pgMar [^>]*/>', pg, s)
        n = _cols(s)
        if n != "1":
            s = re.sub(r'<w:cols [^>]*>.*?</w:cols>|<w:cols [^>]*/>',
                       f'<w:cols w:num="{n}" w:space="0"/>', s, flags=re.S)
        out.append([c, s])
    return out


def _unify_reference_indents(xml: str) -> str:
    """'[12] Yazar, ...' kaynakça girdileri: sayfadaki sütun konumundan gelen farklı
    girintileri en yaygın asılı girintiye eşitle."""
    paras = re.findall(r'<w:p[ >](?:(?!<w:p[ >]).)*?</w:p>', xml, re.S)
    refs = [p for p in paras if re.match(r'\s*\[\d{1,3}\]', _ptext(p))]
    if len(refs) < 4:
        return xml
    inds = Counter(m.group(0) for p in refs for m in [re.search(r'<w:ind [^>]*/>', p)] if m)
    hanging = [i for i, _ in inds.most_common() if "hanging" in i]
    if not hanging:
        return xml
    target = hanging[0]
    for p in set(refs):
        q = re.sub(r'<w:ind [^>]*/>', target, p) if "<w:ind " in p else p
        xml = xml.replace(p, q)
    return xml


def _fit_to_column(xml: str) -> str:
    """Tam sayfa genişliğine göre girintilenmiş paragrafları dar sütuna uydurur."""
    def ind(m):
        a = dict(re.findall(r'w:(\w+)="(-?\d+)"', m.group(0)))
        left = min(int(a.get("left", 0)), 284)
        hang = min(int(a.get("hanging", 0)), left)
        extra = f' w:hanging="{hang}"' if hang else ' w:firstLine="0"'
        return f'<w:ind w:left="{left}" w:right="0"{extra}/>'
    xml = re.sub(r'<w:ind [^>]*/>', ind, xml)
    return re.sub(r'<w:tabs>.*?</w:tabs>', '', xml, flags=re.S)


def _split_at_tab(p: str):
    """'<sol> <w:tab/> <sağ>' tek sekmeli paragrafı iki paragrafa böler; değilse None."""
    tabs = re.findall(r'<w:r(?: [^>]*)?>(?:<w:rPr>(?:(?!</w:r>).)*?</w:rPr>)?<w:tab/></w:r>', p, re.S)
    if len(tabs) != 1:
        return None
    m = re.match(r'(<w:p(?: [^>]*)?>)(<w:pPr>.*?</w:pPr>)?(.*)</w:p>$', p, re.S)
    if not m:
        return None
    head, ppr, body = m.group(1), m.group(2) or "", m.group(3)
    left, right = body.split(tabs[0], 1)
    if not _ptext(left).strip() or not _ptext(right).strip():
        return None
    ppr = re.sub(r'<w:tabs>.*?</w:tabs>', '', ppr, flags=re.S)
    return head + ppr + left + "</w:p>", head + ppr + right + "</w:p>"


def _has_content(xml: str) -> bool:
    return bool(re.search(r'<w:t(?: [^>]*)?>[^<]*\S', xml)) or "<w:drawing" in xml or "<w:tbl>" in xml


def _ptext(p: str) -> str:
    return "".join(re.findall(r'<w:t(?: [^>]*)?>(.*?)</w:t>', p, re.S))


def reflow_sections(xml: str, info: PageInfo) -> str:
    if not info.full:
        return xml
    b0 = xml.find("<w:body>") + len("<w:body>")
    b1 = xml.rfind("</w:body>")
    body = xml[b0:b1]
    fm = None
    for fm in re.finditer(r'<w:sectPr(?: [^>]*)?>', body):
        pass
    if not fm or not body.endswith("</w:sectPr>") or body[:fm.start()].endswith("<w:pPr>"):
        return xml
    final_inner = body[fm.end():-len("</w:sectPr>")]
    # Bölümleri ayır: [(içerik, sectPr_iç)]
    secs, pos = [], 0
    for m in _SECT_P.finditer(body[:fm.start()]):
        secs.append([body[pos:m.start()], m.group(1)])
        pos = m.end()
    secs.append([body[pos:fm.start()], final_inner])

    # Sayfa numarası ata: 'yeni sayfa' ile başlayan her bölüm yeni PDF sayfası
    page_of, page = [], -1
    for _, s in secs:
        if _stype(s) in ("nextPage", "oddPage", "evenPage"):
            page += 1
        page_of.append(max(page, 0))
    if page + 1 != len(info.full):
        return xml  # eşleşmiyor: güvenli tarafta kal
    flow_into = [False] + [info.full[p - 1] for p in range(1, len(info.full))]
    flow_pages = {p for p in range(len(info.full))
                  if flow_into[p] or (p + 1 < len(flow_into) and flow_into[p + 1])}
    if not any(flow_into):
        return xml

    # a) Dolu sayfadan sonraki sayfa: 'sürekli' başlasın
    for k, (_, s) in enumerate(secs):
        if k and page_of[k] != page_of[k - 1] and flow_into[page_of[k]]:
            if "<w:type " in s:
                s = re.sub(r'<w:type w:val="\w+"/>', '<w:type w:val="continuous"/>', s)
            else:
                s = '<w:type w:val="continuous"/>' + s
            secs[k][1] = s

    # b) Akan sayfalarda üst/alt bilgileri sil
    def drop_headers(content: str) -> str:
        def f(m):
            p = m.group(0)
            t = _ptext(p).strip()
            if not t:
                return p
            if _norm(t) in info.headers or (t.isdigit() and t in info.page_numbers):
                return ""
            # 'Dergi adı 342' gibi birleşik satırlar
            if any(h in _norm(t) for h in info.headers if len(h) >= 12) and len(t) < 160:
                return ""
            return p
        return re.sub(r'<w:p[ >](?:(?!<w:p[ >]).)*?</w:p>', f, content, flags=re.S)

    for k in range(len(secs)):
        if page_of[k] in flow_pages:
            secs[k][0] = drop_headers(secs[k][0])

    # b2) İki sütunlu bölümlerin arasında kalmış kısa tek sütunlu 'adalar':
    #     - yan yana başlık satırı ('5. Costs <sekme> Acknowledgements'): sol parça sol
    #       sütunun, sağ parça sağ sütunun başına taşınır
    #     - kısa dipnot vb.: sütun akışına katılır (Word aksi hâlde sütunları dengeler)
    def multi(k):
        return 0 <= k < len(secs) and _cols(secs[k][1]) != "1"

    def next_nonempty(k):
        k += 1
        while k < len(secs) and not _has_content(secs[k][0]):
            k += 1
        return k

    for k in range(1, len(secs) - 1):
        c, sp = secs[k]
        if page_of[k] not in flow_pages or _cols(sp) != "1" or not _has_content(c):
            continue
        if "<w:tbl>" in c or "<w:drawing" in c or len(_ptext(c)) > 300:
            continue
        nk = next_nonempty(k)
        if not (multi(k - 1) or multi(nk)):
            continue
        paras = re.findall(r'<w:p[ >](?:(?!<w:p[ >]).)*?</w:p>', c, re.S)
        split = [_split_at_tab(p) for p in paras]
        if (all(split) and nk + 1 < len(secs) and multi(nk)
                and _stype(secs[nk + 1][1]) == "nextColumn" and multi(nk + 1)):
            secs[nk][0] = _fit_to_column("".join(l for l, _ in split)) + secs[nk][0]
            secs[nk + 1][0] = _fit_to_column("".join(r for _, r in split)) + secs[nk + 1][0]
            secs[k][0] = ""
        elif multi(k - 1) and multi(nk):
            secs[k - 1][0] += _fit_to_column(c)
            secs[k][0] = ""

    # c) Akan sayfalarda: aynı sütun düzenindeki ardışık bölümleri ve üst bilgisi
    #    silinip boş kalan bölümleri birleştir. Sütunlar sayfa sayfa değil, gerçek
    #    bir makale gibi akar; Word gereksiz sütun dengelemesi yapmaz.
    items = [[c, s, page_of[k] in flow_pages] for k, (c, s) in enumerate(secs)]
    changed = True
    while changed:
        changed = False
        for i in range(1, len(items)):
            (pc, ps, pf), (c, s, f) = items[i - 1], items[i]
            if not (pf and f) or _stype(s) not in ("continuous", "nextColumn"):
                continue
            if _cols(ps) != _cols(s) and _has_content(pc):
                continue
            # birleşik bölüm: öncekinin başlangıç türü + sonrakinin sayfa/sütun özellikleri
            t = _stype(ps)
            s = re.sub(r'<w:type w:val="\w+"/>', '', s)
            if t != "nextPage":
                s = f'<w:type w:val="{"continuous" if t == "nextColumn" else t}"/>' + s
            items[i - 1:i + 1] = [[pc + c, s, True]]
            changed = True
            break
    merged = [[c, s] for c, s, _ in items]

    # d) Word, kenar boşlukları değişen 'sürekli' bölüm sonunu SAYFA SONU gibi uygular
    #    (LibreOffice uygulamaz). pdf2docx her PDF sayfasına kendi boşluklarını veriyor
    #    (tek/çift sayfa farkı); akan belgede hepsini eşitle, sütunları eşit genişlik yap.
    merged = _unify_page_setup(merged)
    merged = [[_unify_reference_indents(c) if _cols(s) != "1" else c, s] for c, s in merged]

    parts = []
    for i, (c, s) in enumerate(merged):
        if i < len(merged) - 1:
            parts.append(c + f'<w:p><w:pPr><w:sectPr>{s}</w:sectPr></w:pPr></w:p>')
        else:
            parts.append(c + f'<w:sectPr>{s}</w:sectPr>')
    return xml[:b0] + "".join(parts) + xml[b1:]
