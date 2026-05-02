import re


_PARA_SPLIT = re.compile(r'\n\n|\n(?=\d{1,3}[.\s]|\+\s)')


def _poly_stats(area: dict) -> tuple:
    poly = area.get("polygon", [[0, 0]])
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), max(xs), min(ys), max(ys)


def _sort_areas(areas: list, page_w: float) -> list:
    mid_x = page_w / 2

    def min_y(a):
        return _poly_stats(a)[2]

    def x_center(a):
        x0, x1, _, _ = _poly_stats(a)
        return (x0 + x1) / 2

    # Detect two-column: only main_text pairs, to avoid false positives from side-floating images
    candidates = [a for a in areas if a.get("type") == "main_text"]
    two_col = False
    for i in range(len(candidates)):
        if two_col:
            break
        x0i, x1i, y0i, y1i = _poly_stats(candidates[i])
        ci = (x0i + x1i) / 2
        for j in range(i + 1, len(candidates)):
            x0j, x1j, y0j, y1j = _poly_stats(candidates[j])
            cj = (x0j + x1j) / 2
            y_overlap = y0i < y1j and y0j < y1i
            sides_differ = (ci < mid_x and cj > mid_x) or (ci > mid_x and cj < mid_x)
            if y_overlap and sides_differ:
                two_col = True
                break

    if not two_col:
        return sorted(areas, key=lambda a: (min_y(a), x_center(a)))

    # Column start: topmost y of narrow (column-specific) main_text areas only.
    # Full-width main_text areas (intros, etc.) are excluded so they land in pre.
    col_mains = [
        a for a in areas
        if a.get("type") == "main_text"
        and (_poly_stats(a)[1] - _poly_stats(a)[0]) <= page_w * 0.65
    ]
    col_start = min((min_y(a) for a in col_mains), default=0)

    # Everything above col_start stays in reading order (headers, titles, decorations)
    pre = sorted([a for a in areas if min_y(a) < col_start], key=min_y)

    # Everything from col_start down is split by x-center into left / right column
    in_col = [a for a in areas if min_y(a) >= col_start]
    left_col  = sorted([a for a in in_col if x_center(a) <  mid_x], key=min_y)
    right_col = sorted([a for a in in_col if x_center(a) >= mid_x], key=min_y)

    return pre + left_col + right_col
