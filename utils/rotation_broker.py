"""
RotationBroker — single authority for all rotation and coordinate-transform decisions.

Responsibilities
────────────────
  Detection   detect_orthogonal_rotation(img)  – Tesseract OSD + projection-profile fallback
              detect_skew(img)                 – sub-degree deskew (ScanTailor-style)

  Application apply_to_image(img, rotation, skew) – bake rotation + skew into a PIL image

  Coord math  rotate_point(x, y, rotation, W, H)
              polygon_bbox(polygon)
              polygon_bbox_rotated(polygon, rotation, W, H)
              content_bbox_tuple(cb_dict)

  Page data   effective_rotation(page_data)    – the rotation downstream steps must act on
              reading_dims(page_data)          – (w, h) as the reader sees them

  Area order  sort_areas(areas, page_w, page_h, rotation)
              sort_page_areas(page_data)       – convenience wrapper

  Model prep  prepare_for_model(img_path, content_bbox, max_px)
              normalize_model_result_coords(result, orig_w, orig_h, crop_bbox, gemini)
              clamp_coords(result)

  Extraction  extract_illustration_crop(page_img, polygon, rotation)
"""

from __future__ import annotations

import io
import re
import subprocess
import tempfile
from pathlib import Path

import math

from PIL import Image

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKEW_MAX_ANGLE       = 7.0    # degrees; matches ScanTailor DEFAULT_MAX_ANGLE
SKEW_MIN_CONFIDENCE  = 1.2    # best_score / mean_score - 1 must exceed this
SKEW_DETECT_SIZE     = 400    # max dimension for detection downscale
OSD_CONFIDENCE_THRESHOLD = 2.0


class RotationBroker:
    """Stateless class — all methods are static.  Never instantiate; call directly."""

    # =========================================================================
    # Internal binarization helpers
    # =========================================================================

    @staticmethod
    def _otsu_threshold(hist: list[int], total: int) -> int:
        """Compute Otsu's optimal threshold separating two pixel populations."""
        sum_all = sum(i * hist[i] for i in range(256))
        sum_bg, weight_bg, best_var, best_t = 0, 0, 0.0, 0
        for t in range(256):
            weight_bg += hist[t]
            if weight_bg == 0 or weight_bg == total:
                continue
            weight_fg = total - weight_bg
            sum_bg += t * hist[t]
            mean_bg = sum_bg / weight_bg
            mean_fg = (sum_all - sum_bg) / weight_fg
            var = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
            if var > best_var:
                best_var, best_t = var, t
        return best_t

    @staticmethod
    def _is_inverted(gray: Image.Image) -> bool:
        """True if the image has a predominantly dark background (light text on dark)."""
        tiny = gray.resize((1, 1), Image.BOX)
        return tiny.getpixel((0, 0)) < 128

    @staticmethod
    def _sauvola_binarize(gray: Image.Image, window: int = 51, k: float = 0.3) -> Image.Image:
        """Sauvola local adaptive binarisation.

        Returns a binary image with foreground (text) = 255, background = 0.
        Falls back to Otsu when numpy is unavailable.
        """
        try:
            import numpy as np
        except ImportError:
            hist = gray.histogram()
            t = RotationBroker._otsu_threshold(hist, gray.width * gray.height)
            return gray.point(lambda p: 255 if p <= t else 0)

        arr = np.array(gray, dtype=np.float64)
        h, w = arr.shape
        pad  = window // 2
        arr_p = np.pad(arr, pad, mode="reflect")

        S  = np.zeros((h + 2 * pad + 1, w + 2 * pad + 1), dtype=np.float64)
        S2 = np.zeros_like(S)
        S[1:, 1:]  = np.cumsum(np.cumsum(arr_p,      axis=0), axis=1)
        S2[1:, 1:] = np.cumsum(np.cumsum(arr_p ** 2, axis=0), axis=1)

        yy = np.arange(h)[:, None]
        xx = np.arange(w)[None, :]
        n  = float(window * window)

        sum1 = S[yy + window, xx + window] - S[yy, xx + window] - S[yy + window, xx] + S[yy, xx]
        sum2 = S2[yy + window, xx + window] - S2[yy, xx + window] - S2[yy + window, xx] + S2[yy, xx]

        local_mean = sum1 / n
        local_std  = np.sqrt(np.maximum(0.0, sum2 / n - local_mean ** 2))

        threshold = local_mean * (1.0 + k * (local_std / 128.0 - 1.0))
        binary    = np.where(arr <= threshold, 255, 0).astype(np.uint8)
        return Image.fromarray(binary, mode="L")

    # =========================================================================
    # Detection
    # =========================================================================

    @staticmethod
    def _detect_rotation_tesseract(pil_img: Image.Image) -> int | None:
        """Return 90/270 via Tesseract OSD, or None if confidence is too low."""
        tmp = Path(tempfile.mktemp(suffix=".png"))
        try:
            pil_img.save(str(tmp))
            result = subprocess.run(
                ["tesseract", str(tmp), "stdout", "--psm", "0"],
                capture_output=True, text=True, timeout=30,
            )
            rotate_m = re.search(r"Rotate:\s*(\d+)",                   result.stdout)
            conf_m   = re.search(r"Orientation confidence:\s*([\d.]+)", result.stdout)
            if rotate_m and conf_m and float(conf_m.group(1)) >= OSD_CONFIDENCE_THRESHOLD:
                # Tesseract gives CW degrees-to-fix; PIL rotate() is CCW.
                rotation = (360 - int(rotate_m.group(1))) % 360
                if rotation in (90, 270):
                    return rotation
        except Exception:
            pass
        finally:
            tmp.unlink(missing_ok=True)
        return None

    @staticmethod
    def _detect_orientation_projection(pil_img: Image.Image) -> int | None:
        """Projection-profile fallback for 90°/180°/270° detection.

        Step 1 — row-projection score identifies whether a quarter-turn is needed
                 (90° and 270° both score identically; the pair wins over 0°/180°).
        Step 2 — tiebreaker for 90 vs 270 uses a magnitude-gated top-heavy heuristic:
                   • weak signal  (< CONFIDENT_MIN) → ambiguous, default to 270° (CW).
                   • moderate sig (CONFIDENT_MIN … CONFIDENT_MAX) → trust it.
                   • strong signal (> CONFIDENT_MAX) → suspect content asymmetry
                     (illustration / table at page bottom), default to 270° (CW).
                 270° is the common scanner convention (page-top at left edge of landscape).
        """
        w, h  = pil_img.size
        scale = min(1.0, SKEW_DETECT_SIZE / max(w, h))
        small = (pil_img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
                 if scale < 1.0 else pil_img.copy())

        gray = small.convert("L")
        if RotationBroker._is_inverted(gray):
            gray = gray.point(lambda p: 255 - p)
        hist      = gray.histogram()
        threshold = RotationBroker._otsu_threshold(hist, gray.width * gray.height)
        binary    = gray.point(lambda p: 255 if p <= threshold else 0)

        def _row_score(angle: int) -> float:
            rot  = binary.rotate(angle, expand=True)
            _, rh = rot.size
            proj = rot.resize((1, rh), Image.BOX)
            vals = proj.get_flattened_data()
            n    = len(vals)
            return sum(v * v for v in vals) / n if n else 0.0

        def _top_heavy(angle: int) -> float:
            rot       = binary.rotate(angle, expand=True)
            rw, rh    = rot.size
            band      = max(1, rh * 15 // 100)
            top       = rot.crop((0, 0, rw, band))
            bot       = rot.crop((0, rh - band, rw, rh))
            area      = rw * band
            return sum(top.get_flattened_data()) / area - sum(bot.get_flattened_data()) / area

        scores = {r: _row_score(r) for r in (0, 90, 180, 270)}
        best   = max(scores, key=scores.__getitem__)

        if best == 0 or scores[best] < scores[0] * 1.05:
            # No clear portrait signal — if the image is landscape, assume 270° CW.
            w, h = pil_img.size
            return 270 if w > h else None

        partner = {90: 270, 270: 90, 180: 0, 0: 180}[best]

        if partner == 0:
            # 180° vs 0° — trust top-heavy signal directly.
            if _top_heavy(best) < _top_heavy(0):
                return 0
            return best

        # 90° vs 270° — magnitude-gated tiebreaker.
        # scores[90] ≈ scores[270] always (row-projection symmetry); max() returns 90
        # first (dict order).  We override to 270 unless a moderate-confidence top-heavy
        # signal says 90° really is correct.
        CONFIDENT_MIN = 1.5
        CONFIDENT_MAX = 5.0
        th_diff = _top_heavy(best) - _top_heavy(partner)  # positive → best is more top-heavy

        if best == 90 and CONFIDENT_MIN <= th_diff <= CONFIDENT_MAX:
            return 90   # moderate signal: 90° CCW is reliably more top-heavy → trust it
        return 270      # default: CW rotation (270° CCW) for all other cases

    @staticmethod
    def detect_orthogonal_rotation(pil_img: Image.Image) -> int | None:
        """Detect the 90/180/270° correction needed to make the page upright.

        Tries Tesseract OSD first; falls back to the projection-profile method when
        Tesseract lacks confidence or fails entirely.  Returns None if no rotation
        is needed or detectable.
        """
        return (RotationBroker._detect_rotation_tesseract(pil_img)
                or RotationBroker._detect_orientation_projection(pil_img))

    @staticmethod
    def detect_skew(pil_img: Image.Image) -> float | None:
        """Detect fine page skew using the projection-profile method (ScanTailor SkewFinder-style).

        Returns the CCW rotation angle (degrees) needed to correct the skew, or None
        if confidence is too low or the angle is negligible (< 0.1°).
        """
        w, h  = pil_img.size
        scale = min(1.0, SKEW_DETECT_SIZE / max(w, h))
        small = (pil_img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
                 if scale < 1.0 else pil_img.copy())

        gray = small.convert("L")
        if RotationBroker._is_inverted(gray):
            gray = gray.point(lambda p: 255 - p)

        hist      = gray.histogram()
        threshold = RotationBroker._otsu_threshold(hist, gray.width * gray.height)
        binary    = gray.point(lambda p: 255 if p <= threshold else 0)
        _, bh     = binary.size

        def _score(angle: float) -> float:
            rot  = binary.rotate(angle, resample=Image.BICUBIC, expand=False)
            proj = rot.resize((1, bh), Image.BOX)
            vals = proj.get_flattened_data()
            return sum((vals[i] - vals[i - 1]) ** 2 for i in range(1, len(vals)))

        coarse     = [(float(a), _score(float(a))) for a in range(-int(SKEW_MAX_ANGLE), int(SKEW_MAX_ANGLE) + 1)]
        best_angle, best_score = max(coarse, key=lambda x: x[1])
        mean_score = sum(s for _, s in coarse) / len(coarse)

        if mean_score <= 0 or (best_score / mean_score - 1.0) < SKEW_MIN_CONFIDENCE:
            return None

        lo, hi = best_angle - 1.0, best_angle + 1.0
        for _ in range(8):
            m1, m2 = lo + (hi - lo) / 3, hi - (hi - lo) / 3
            if _score(m1) >= _score(m2):
                hi = m2
            else:
                lo = m1

        fine = round((lo + hi) / 2, 2)
        return fine if abs(fine) >= 0.1 else None

    # =========================================================================
    # Application
    # =========================================================================

    @staticmethod
    def apply_to_image(
        img:        Image.Image,
        rotation:   int   | None = None,
        skew_angle: float | None = None,
    ) -> Image.Image:
        """Apply orthogonal rotation then fine skew correction to a PIL image.

        Both transforms are applied in-place (returns a new image; caller keeps
        or discards the original).  Either argument may be None / 0 to skip.
        """
        if rotation:
            img = img.rotate(rotation, expand=True)
        if skew_angle:
            img = img.rotate(skew_angle, resample=Image.BICUBIC, expand=True)
        return img

    # =========================================================================
    # Coordinate math
    # =========================================================================

    @staticmethod
    def rotate_point(
        x: float, y: float,
        rotation: int,
        page_w: float, page_h: float,
    ) -> tuple[float, float]:
        """Rotate coordinate (x, y) CCW by *rotation* degrees within a W×H page."""
        if rotation == 90:  return (y, page_w - x)
        if rotation == 180: return (page_w - x, page_h - y)
        if rotation == 270: return (page_h - y, x)
        return (x, y)

    @staticmethod
    def _rotate_bbox_cw(
        bbox: tuple[int, int, int, int],
        rotation: int,
        raw_w: int, raw_h: int,
    ) -> tuple[int, int, int, int]:
        """Transform a content_bbox from raw space to CW-rotated image space.

        Matches the CW rotation applied by the review UI (rotPt) and by
        prepare_for_model when rotation is non-zero.
        """
        l, t, r, b = bbox
        if rotation == 90:  return (raw_h - b, l,         raw_h - t, r        )
        if rotation == 180: return (raw_w - r, raw_h - b, raw_w - l, raw_h - t)
        if rotation == 270: return (t,         raw_w - r, b,         raw_w - l)
        return bbox

    @staticmethod
    def unrotate_point(
        x: float, y: float,
        rotation: int,
        raw_w: int, raw_h: int,
    ) -> tuple[float, float]:
        """Inverse of rotPt in review.html: converts CW-rotated coords back to raw space."""
        if rotation == 90:  return (y,         raw_h - x)
        if rotation == 180: return (raw_w - x, raw_h - y)
        if rotation == 270: return (raw_w - y, x        )
        return (x, y)

    @staticmethod
    def polygon_bbox(polygon: list) -> tuple[int, int, int, int]:
        """(x0, y0, x1, y1) bounding box of *polygon*."""
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def polygon_bbox_rotated(
        polygon: list,
        rotation: int,
        page_w: float, page_h: float,
    ) -> tuple[float, float, float, float]:
        """Bounding box of *polygon* after rotating CCW by *rotation* degrees.

        Gives coordinates in the reading space, i.e. as the reader sees the page.
        """
        pts = [RotationBroker.rotate_point(x, y, rotation, page_w, page_h)
               for x, y in polygon]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return min(xs), max(xs), min(ys), max(ys)

    @staticmethod
    def content_bbox_tuple(cb: dict | None) -> tuple[int, int, int, int] | None:
        """Convert a content-bbox dict to a (left, top, right, bottom) tuple, or None."""
        if not cb:
            return None
        try:
            return cb["left"], cb["top"], cb["right"], cb["bottom"]
        except KeyError:
            return None

    # =========================================================================
    # Page-level accessors
    # =========================================================================

    @staticmethod
    def effective_rotation(page_data: dict) -> int:
        """Rotation that downstream steps (sorting, illustration extraction) must apply.

        ``rotation_applied`` means step1 already baked the rotation into the PNG, so
        step2 detected areas in the already-corrected image space — no further transform
        needed downstream.

        ``rotation`` is a manual override set via the UI for pages where step2 ran on
        an un-corrected image.  Downstream steps must use this to interpret area coords.
        """
        return int(page_data.get("rotation") or 0)

    @staticmethod
    def reading_dims(page_data: dict) -> tuple[float, float]:
        """(width, height) as the reader sees them, accounting for effective rotation."""
        dims = page_data.get("page_dimensions") or {}
        w    = float(dims.get("width")  or 0)
        h    = float(dims.get("height") or 0)
        rot  = RotationBroker.effective_rotation(page_data)
        if rot in (90, 270):
            return h, w
        return w, h

    # =========================================================================
    # Area sorting  (full algorithm from shared_layout)
    # =========================================================================

    @staticmethod
    def sort_areas(
        areas:   list,
        page_w:  float,
        page_h:  float = 0,
        rotation: int  = 0,
    ) -> list:
        """Return *areas* sorted in reading order, accounting for *rotation*.

        Handles two-column layouts and side-floating illustrations.
        """
        rW = page_h if rotation in (90, 270) else page_w

        def _stats(a: dict):
            poly = a.get("polygon", [[0, 0]])
            if rotation:
                return RotationBroker.polygon_bbox_rotated(poly, rotation, page_w, page_h)
            xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
            return min(xs), max(xs), min(ys), max(ys)

        mid_x = rW / 2

        def _min_y(a):  return _stats(a)[2]
        def _cx(a):
            x0, x1, _, _ = _stats(a)
            return (x0 + x1) / 2

        # Detect two-column via main_text pairs
        candidates = [a for a in areas if a.get("type") == "main_text"]
        two_col = False
        for i in range(len(candidates)):
            if two_col:
                break
            x0i, x1i, y0i, y1i = _stats(candidates[i])
            ci = (x0i + x1i) / 2
            for j in range(i + 1, len(candidates)):
                x0j, x1j, y0j, y1j = _stats(candidates[j])
                cj = (x0j + x1j) / 2
                y_overlap   = y0i < y1j and y0j < y1i
                sides_differ = (ci < mid_x and cj > mid_x) or (ci > mid_x and cj < mid_x)
                if y_overlap and sides_differ:
                    two_col = True
                    break

        # Secondary: side-floating illustration alongside text
        if not two_col:
            for illus in areas:
                if illus.get("type") != "illustration":
                    continue
                x0i, x1i, y0i, y1i = _stats(illus)
                ci = (x0i + x1i) / 2
                if abs(ci - mid_x) < rW * 0.1:
                    continue
                for txt in areas:
                    if txt.get("type") not in {"main_text", "chapter_title", "subtitle"}:
                        continue
                    x0t, x1t, y0t, y1t = _stats(txt)
                    ct = (x0t + x1t) / 2
                    if (ci < mid_x) == (ct < mid_x):
                        continue
                    if y0i < y1t and y0t < y1i:
                        two_col = True
                        break
                if two_col:
                    break

        if not two_col:
            return sorted(areas, key=lambda a: (_min_y(a), _cx(a)))

        # Two-column split
        col_mains  = [a for a in areas
                      if a.get("type") == "main_text"
                      and (_stats(a)[1] - _stats(a)[0]) <= rW * 0.65]
        col_start  = min((_min_y(a) for a in col_mains), default=0)

        def _is_side_float(a):
            if a.get("type") != "illustration":
                return False
            x0, x1, _, _ = _stats(a)
            return (x1 - x0) <= rW * 0.65 and abs((x0 + x1) / 2 - mid_x) > rW * 0.1

        pre       = sorted([a for a in areas if _min_y(a) < col_start and not _is_side_float(a)],
                            key=_min_y)
        in_col    = [a for a in areas if _min_y(a) >= col_start or _is_side_float(a)]
        left_col  = sorted([a for a in in_col if _cx(a) <  mid_x], key=_min_y)
        right_col = sorted([a for a in in_col if _cx(a) >= mid_x], key=_min_y)

        return pre + left_col + right_col

    @staticmethod
    def sort_page_areas(page_data: dict) -> list:
        """Convenience: sort areas from *page_data* using its dimensions and effective rotation."""
        dims = page_data.get("page_dimensions") or {}
        w    = float(dims.get("width")  or 1000)
        h    = float(dims.get("height") or 1000)
        rot  = RotationBroker.effective_rotation(page_data)
        return RotationBroker.sort_areas(page_data.get("areas") or [], w, h, rot)

    # =========================================================================
    # Model submission
    # =========================================================================

    @staticmethod
    def prepare_for_model(
        img_path:    Path,
        content_bbox: tuple[int, int, int, int] | None = None,
        max_px:      int = 1500,
    ) -> tuple[bytes, int, int]:
        """Load page image, crop to content bbox (optional), scale, JPEG-encode.

        Returns (jpeg_bytes, orig_w, orig_h) where orig_w/h are always the full
        page dimensions (before any crop), so coordinate transforms can reference them.
        """
        img = Image.open(img_path).convert("L").convert("RGB")
        orig_w, orig_h = img.size
        if content_bbox:
            img = img.crop(content_bbox)
        send_w, send_h = img.size
        scale = min(max_px / send_w, max_px / send_h, 1.0)
        if scale < 1.0:
            img = img.resize((int(send_w * scale), int(send_h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue(), orig_w, orig_h

    @staticmethod
    def normalize_model_result_coords(
        result:                  dict,
        orig_w:                  int,
        orig_h:                  int,
        crop_bbox:               tuple[int, int, int, int] | None = None,
        uses_gemini_normalization: bool = False,
    ) -> None:
        """Convert model output coordinates to original page pixel space.  Mutates *result*.

        Gemini 2.x returns 0-1000 normalised coords.
        Gemini 3.x+ reports its own page_dimensions and returns coords in that space.
        Other backends (Claude etc.) return pixel coords in the image they received.
        """
        if uses_gemini_normalization:
            all_coords = [
                c
                for area in result.get("areas", [])
                for pt in area.get("polygon", [])
                for c in pt
            ]
            if max(all_coords, default=0) <= 1000:
                # Classic 0-1000 Gemini normalization — clamp to [0,1000] then scale
                if crop_bbox:
                    left, top, right, bottom = crop_bbox
                    cw, ch = right - left, bottom - top
                    for area in result.get("areas", []):
                        area["polygon"] = [
                            [round(max(0, min(1000, x)) / 1000 * cw) + left,
                             round(max(0, min(1000, y)) / 1000 * ch) + top]
                            for x, y in area["polygon"]
                        ]
                else:
                    for area in result.get("areas", []):
                        area["polygon"] = [
                            [round(max(0, min(1000, x)) / 1000 * orig_w),
                             round(max(0, min(1000, y)) / 1000 * orig_h)]
                            for x, y in area["polygon"]
                        ]
                result["page_dimensions"] = {"width": orig_w, "height": orig_h}
                return
            # Gemini 3.x: coords exceed 1000 — fall through to model-dims scaling below

        # Non-Gemini (pixel coords) or Gemini with model-specific coordinate scale.
        # Model reports page_dimensions matching its coord space; clamp before scaling
        # because models routinely overshoot their own reported bounds.
        model_dims = result.get("page_dimensions") or {}
        model_w    = model_dims.get("width")  or orig_w
        model_h    = model_dims.get("height") or orig_h
        if crop_bbox:
            left, top, right, bottom = crop_bbox
            cw, ch = right - left, bottom - top
            for area in result.get("areas", []):
                area["polygon"] = [
                    [round(max(0, min(model_w, x)) * cw / model_w) + left,
                     round(max(0, min(model_h, y)) * ch / model_h) + top]
                    for x, y in area["polygon"]
                ]
        else:
            for area in result.get("areas", []):
                area["polygon"] = [
                    [round(max(0, min(model_w, x)) * orig_w / model_w),
                     round(max(0, min(model_h, y)) * orig_h / model_h)]
                    for x, y in area["polygon"]
                ]

        result["page_dimensions"] = {"width": orig_w, "height": orig_h}

    @staticmethod
    def clamp_coords(result: dict) -> None:
        """Clamp all polygon vertices to the page boundary declared in *result*.  Mutates."""
        dims = result.get("page_dimensions") or {}
        w, h = dims.get("width", 0), dims.get("height", 0)
        if not w or not h:
            return
        for area in result.get("areas", []):
            area["polygon"] = [
                [max(0, min(w, x)), max(0, min(h, y))]
                for x, y in area["polygon"]
            ]

    # =========================================================================
    # Illustration extraction
    # =========================================================================

    @staticmethod
    def extract_illustration_crop(
        page_img:   Image.Image,
        polygon:    list,
        rotation:   int   = 0,
        skew_angle: float = 0,
    ) -> Image.Image:
        """Crop the bounding box of *polygon* from *page_img*, then apply *rotation* and *skew_angle*.

        *rotation* should come from ``effective_rotation(page_data)`` — it is non-zero
        only when step2 ran on an un-corrected image and the crop coordinates are in
        the unrotated space.

        *skew_angle* should come from ``page_data.get('skew_angle')`` — it is the
        fine deskew correction applied client-side in the review UI but not baked into
        the page PNG, so it must be applied here to straighten the crop.

        Skew must be applied to the FULL page before cropping so the crop is taken
        from already-deskewed pixels (no black background corners).  PIL rotates the
        image around its center, so the polygon vertices must be rotated around the
        same center to find where the original content lands in the deskewed image.
        We use ``expand=False`` to match the review UI canvas (which sizes itself by
        rotation only, not skew); the only thing clipped is the very corners of the
        page, which are page background, not illustration content.
        """
        if skew_angle:
            w, h = page_img.size
            cx, cy = w / 2, h / 2
            page_img = page_img.rotate(skew_angle, resample=Image.BICUBIC, expand=False)
            theta = math.radians(skew_angle)
            cos_t, sin_t = math.cos(theta), math.sin(theta)
            rotated = []
            for x, y in polygon:
                dx, dy = x - cx, y - cy
                rotated.append((cos_t * dx + sin_t * dy + cx,
                                -sin_t * dx + cos_t * dy + cy))
            xs = [p[0] for p in rotated]
            ys = [p[1] for p in rotated]
            orig_x0, orig_y0, orig_x1, orig_y1 = RotationBroker.polygon_bbox(polygon)
            box = (max(round(min(xs)), orig_x0), max(round(min(ys)), orig_y0),
                   min(round(max(xs)), orig_x1), min(round(max(ys)), orig_y1))
        else:
            box = RotationBroker.polygon_bbox(polygon)
        cropped = page_img.crop(box)
        if rotation:
            cropped = cropped.rotate(rotation, expand=True)
        return cropped
