"""Photo to document: turns a phone photo of a sheet of paper into a scanned document.

  1) Find the four corners of the paper (edges + contours; otherwise the largest bright region)
  2) Correct the perspective (top-down rectangle)
  3) Even out the light, remove shadows, whiten the paper (greyscale; pencil marks are not lost)
  4) Place it on an A4 page; add an invisible OCR text layer if Tesseract is available

OpenCV already comes installed with pdf2docx (opencv-python-headless, Apache-2.0).
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from .i18n import T

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

A4 = (595.0, 842.0)
WORK = 1200       # downscaled size used for the corner search (px)
MAX_OUT = 2480    # long side of the output (A4 @ 300 dpi ≈ 3508; 2480 ≈ 210 dpi, size/quality balance)


def _order(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    pts = pts.reshape(4, 2).astype("float32")
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype="float32")


def find_corners(bgr: np.ndarray) -> np.ndarray | None:
    """Corners of the paper (in full-resolution coordinates) or None.

    Quadrilateral candidates are taken from several masks and scored:
      - edges (after a strong blur to suppress noise)
      - brightness (Otsu) and colour saturation (paper is colourless; a desk/fabric is coloured,
        and this is not affected by light changes)
    Quadrilaterals touching the edge of the photo are penalised.
    """
    h, w = bgr.shape[:2]
    scale = WORK / max(h, w)
    small = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    blur = cv2.medianBlur(cv2.GaussianBlur(gray, (5, 5), 0), 7)
    hsv = cv2.cvtColor(cv2.medianBlur(small, 7), cv2.COLOR_BGR2HSV)
    k = np.ones((9, 9), np.uint8)
    masks = []
    edges = cv2.dilate(cv2.Canny(blur, 40, 120), np.ones((5, 5), np.uint8), iterations=2)
    masks.append(edges)
    _, bright = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    masks.append(cv2.morphologyEx(bright, cv2.MORPH_OPEN, k))
    _, lowsat = cv2.threshold(hsv[:, :, 1], 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    masks.append(cv2.morphologyEx(cv2.morphologyEx(lowsat, cv2.MORPH_OPEN, k), cv2.MORPH_CLOSE, k))
    both = cv2.bitwise_and(masks[1], masks[2])
    masks.append(cv2.morphologyEx(both, cv2.MORPH_CLOSE, k))

    area_min = 0.15 * sh * sw
    margin = 0.01 * max(sh, sw)
    best, best_score = None, 0.0
    for mask in masks:
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5]:
            a = cv2.contourArea(c)
            if a < area_min:
                break
            hull = cv2.convexHull(c)
            peri = cv2.arcLength(hull, True)
            for eps in (0.015, 0.02, 0.03, 0.05):
                approx = cv2.approxPolyDP(hull, eps * peri, True)
                if len(approx) == 4:
                    break
            else:
                continue
            quad = approx.reshape(4, 2).astype("float32")
            qa = cv2.contourArea(quad)
            touching = sum(1 for x, y in quad if x < margin or y < margin or x > sw - margin or y > sh - margin)
            fill = a / max(qa, 1)                     # how much of the quadrilateral the contour fills
            score = qa * fill * (0.25 ** touching)
            if score > best_score:
                best, best_score = quad, score
    if best is None:
        return None
    corners = _order(best) / scale
    if cv2.contourArea(corners.astype("float32")) > 0.97 * h * w:
        return None                                   # photo already cropped
    return corners


def true_aspect(corners: np.ndarray, w: int, h: int) -> float | None:
    """REAL width/height ratio of a rectangle photographed at an angle.

    Zhang & He, "Whiteboard scanning and image enhancement" (2007): the camera's focal length (f)
    and the real aspect of the plane are computed from the four corners; the principal point
    is assumed to be the image centre. None if the computation is unstable (falls back to the edge ratio).
    """
    tl, tr, br, bl = corners.astype("float64")
    u0, v0 = w / 2.0, h / 2.0
    m1, m2, m3, m4 = (np.array([*p, 1.0]) for p in (tl, tr, bl, br))
    try:
        k2 = np.dot(np.cross(m1, m4), m3) / np.dot(np.cross(m2, m4), m3)
        k3 = np.dot(np.cross(m1, m4), m2) / np.dot(np.cross(m3, m4), m2)
    except ZeroDivisionError:
        return None
    n2, n3 = k2 * m2 - m1, k3 * m3 - m1
    n21, n22, n23 = n2
    n31, n32, n33 = n3
    if abs(n23) < 1e-6 and abs(n33) < 1e-6:
        return None                                  # shot exactly head-on: the edge ratio is already right
    f2 = None
    if abs(n23 * n33) > 1e-12:
        f2 = -((n21 * n31 - (n21 * n33 + n23 * n31) * u0 + n23 * n33 * u0 * u0)
               + (n22 * n32 - (n22 * n33 + n23 * n32) * v0 + n23 * n33 * v0 * v0)) / (n23 * n33)
    if f2 is None or not np.isfinite(f2) or f2 <= 0:
        # One-directional perspective: f can't be derived from the corners → typical phone camera (~26 mm equivalent)
        f2 = (0.8 * max(w, h)) ** 2
    f = np.sqrt(f2)
    A = np.array([[f, 0, u0], [0, f, v0], [0, 0, 1.0]])
    Ai = np.linalg.inv(A)
    B = Ai.T @ Ai
    r2 = (n2 @ B @ n2) / (n3 @ B @ n3)
    if not np.isfinite(r2) or r2 <= 0:
        return None
    r = float(np.sqrt(r2))
    return r if 0.2 < r < 5 else None


A4_RATIO = 210 / 297


def warp(bgr: np.ndarray, corners: np.ndarray) -> np.ndarray:
    tl, tr, br, bl = corners
    wid = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    hei = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    ratio = true_aspect(corners, bgr.shape[1], bgr.shape[0])
    if ratio is not None:
        # within 8% of A4 (portrait or landscape): snap to the exact A4 ratio
        for a4 in (A4_RATIO, 1 / A4_RATIO):
            if abs(ratio - a4) / a4 < 0.08:
                ratio = a4
        hei = int(max(wid, hei) / max(ratio, 1e-6)) if ratio < 1 else hei
        wid = int(hei * ratio)
    dst = np.array([[0, 0], [wid - 1, 0], [wid - 1, hei - 1], [0, hei - 1]], dtype="float32")
    m = cv2.getPerspectiveTransform(corners, dst)
    return cv2.warpPerspective(bgr, m, (wid, hei), flags=cv2.INTER_CUBIC)


def enhance(bgr: np.ndarray) -> np.ndarray:
    """Shadow/light equalization: divide by the background (large blur) to pull the paper to white."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    k = max(31, (min(gray.shape) // 20) | 1)
    bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    bg = cv2.GaussianBlur(bg, (k, k), 0)
    norm = cv2.divide(gray, bg, scale=255)
    # contrast: paper white, text dark; midtones are kept
    lo, hi = np.percentile(norm, 1), np.percentile(norm, 60)
    out = np.clip((norm.astype("float32") - lo) * 255.0 / max(hi - lo, 1), 0, 255).astype("uint8")
    return out


def photo_to_page_image(path: Path) -> tuple[bytes, tuple[int, int], bool]:
    """(JPEG bytes, (width, height), whether corners were found)"""
    if cv2 is None:
        from .actions import ConversionError
        raise ConversionError(T("Fotoğraftan belge için OpenCV gerekli: pip install opencv-python-headless"))
    from .tools import _open_image
    rgb = np.asarray(_open_image(path))            # HEIC + EXIF orientation
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    corners = find_corners(bgr)
    doc = warp(bgr, corners) if corners is not None else bgr
    h, w = doc.shape[:2]
    if corners is not None:                      # drop the thin strip of desk left at the edge
        m = max(3, int(0.015 * min(h, w)))
        doc = doc[m:h - m, m:w - m]
        h, w = doc.shape[:2]
    if max(h, w) > MAX_OUT:
        z = MAX_OUT / max(h, w)
        doc = cv2.resize(doc, (int(w * z), int(h * z)), interpolation=cv2.INTER_AREA)
    clean = enhance(doc)
    if corners is not None:
        # whiten light-grey desk leftovers in the edge band (dark text/signatures are kept)
        bh, bw = clean.shape[:2]
        b = max(4, int(0.025 * min(bh, bw)))
        for sl in (np.s_[:b, :], np.s_[-b:, :], np.s_[:, :b], np.s_[:, -b:]):
            band = clean[sl]
            band[band > 150] = 255
    ok, buf = cv2.imencode(".jpg", clean, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return buf.tobytes(), (clean.shape[1], clean.shape[0]), corners is not None


def photos_to_pdf(paths: list[Path], out_path: Path, progress=lambda m: None, ocr: bool = True) -> dict:
    from .actions import pymupdf
    out = pymupdf.open()
    found = 0
    for i, p in enumerate(paths):
        progress(T("Belge hazırlanıyor: {0} ({1}/{2})", p.name, i + 1, len(paths)))
        data, (w, h), ok = photo_to_page_image(p)
        found += ok
        W, H = A4 if h >= w else (A4[1], A4[0])
        page = out.new_page(width=W, height=H)
        m = 14.0  # ~5 mm edge
        page.insert_image(pymupdf.Rect(m, m, W - m, H - m), stream=data, keep_proportion=True)
    words = 0
    if ocr:
        from . import ocr as ocrmod
        try:
            wb = ocrmod.ocr_pages(out, list(range(len(out))), progress)
            words = ocrmod.make_searchable(out, wb)
        except ocrmod.OcrUnavailable:
            words = -1
    out.save(out_path, garbage=3, deflate=True)
    out.close()
    return {"pages": len(paths), "corners": found, "words": words}
