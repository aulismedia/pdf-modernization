import re


_PARA_SPLIT = re.compile(r'\n\n|\n(?=\d{1,3}[.\s]|\+\s)')


def _poly_stats(area: dict) -> tuple:
    poly = area.get("polygon", [[0, 0]])
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), max(xs), min(ys), max(ys)


def _rotate_point(x: float, y: float, rotation: int, W: float, H: float) -> tuple:
    if rotation == 90:  return (y, W - x)
    if rotation == 180: return (W - x, H - y)
    if rotation == 270: return (H - y, x)
    return (x, y)


def _poly_stats_rotated(area: dict, rotation: int, W: float, H: float) -> tuple:
    poly = area.get("polygon", [[0, 0]])
    pts = [_rotate_point(x, y, rotation, W, H) for x, y in poly]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), max(xs), min(ys), max(ys)


def _sort_areas(areas: list, page_w: float, page_h: float = 0, rotation: int = 0) -> list:
    # Use rotated coordinate space so "above/below" reflects what the reader sees.
    rW = page_h if rotation in (90, 270) else page_w

    def stats(a):
        return _poly_stats_rotated(a, rotation, page_w, page_h) if rotation else _poly_stats(a)

    mid_x = rW / 2

    def min_y(a):
        return stats(a)[2]

    def x_center(a):
        x0, x1, _, _ = stats(a)
        return (x0 + x1) / 2

    # Detect two-column: only main_text pairs, to avoid false positives from side-floating images
    candidates = [a for a in areas if a.get("type") == "main_text"]
    two_col = False
    for i in range(len(candidates)):
        if two_col:
            break
        x0i, x1i, y0i, y1i = stats(candidates[i])
        ci = (x0i + x1i) / 2
        for j in range(i + 1, len(candidates)):
            x0j, x1j, y0j, y1j = stats(candidates[j])
            cj = (x0j + x1j) / 2
            y_overlap = y0i < y1j and y0j < y1i
            sides_differ = (ci < mid_x and cj > mid_x) or (ci > mid_x and cj < mid_x)
            if y_overlap and sides_differ:
                two_col = True
                break

    # Secondary: illustration clearly on one side + text/title on the other side with vertical overlap.
    # Only fires when the primary text-pair check found nothing.
    if not two_col:
        for illus in areas:
            if illus.get("type") != "illustration":
                continue
            x0i, x1i, y0i, y1i = stats(illus)
            ci = (x0i + x1i) / 2
            if abs(ci - mid_x) < rW * 0.1:  # skip near-centred illustrations
                continue
            for txt in areas:
                if txt.get("type") not in {"main_text", "chapter_title", "subtitle"}:
                    continue
                x0t, x1t, y0t, y1t = stats(txt)
                ct = (x0t + x1t) / 2
                if (ci < mid_x) == (ct < mid_x):  # same side
                    continue
                if y0i < y1t and y0t < y1i:        # vertical overlap
                    two_col = True
                    break
            if two_col:
                break

    if not two_col:
        return sorted(areas, key=lambda a: (min_y(a), x_center(a)))

    # Column start: topmost y of narrow (column-specific) main_text areas only.
    # Full-width main_text areas (intros, etc.) are excluded so they land in pre.
    col_mains = [
        a for a in areas
        if a.get("type") == "main_text"
        and (stats(a)[1] - stats(a)[0]) <= rW * 0.65
    ]
    col_start = min((min_y(a) for a in col_mains), default=0)

    # A narrow illustration clearly on one side is a side float — it belongs in the
    # column split even if its top edge is above col_start (e.g. a full-height photo
    # on the right alongside text on the left).
    def _is_side_float(a):
        if a.get("type") != "illustration":
            return False
        x0, x1, _, _ = stats(a)
        return (x1 - x0) <= rW * 0.65 and abs((x0 + x1) / 2 - mid_x) > rW * 0.1

    # Everything above col_start stays in reading order (headers, titles, decorations),
    # unless it's a side-float illustration which belongs in the column split.
    pre    = sorted([a for a in areas if min_y(a) < col_start and not _is_side_float(a)], key=min_y)

    # Everything from col_start down, plus any side-float illustrations, split by x-center.
    in_col = [a for a in areas if min_y(a) >= col_start or _is_side_float(a)]
    left_col  = sorted([a for a in in_col if x_center(a) <  mid_x], key=min_y)
    right_col = sorted([a for a in in_col if x_center(a) >= mid_x], key=min_y)

    return pre + left_col + right_col
