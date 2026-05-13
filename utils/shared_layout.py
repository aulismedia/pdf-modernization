import re

from utils.rotation_broker import RotationBroker

_PARA_SPLIT = re.compile(r'\n\n|\n(?=\d{1,3}[.\s]|\+\s)')

# Re-export for callers that import these directly from shared_layout.
_rotate_point        = RotationBroker.rotate_point
_sort_areas          = RotationBroker.sort_areas


def _poly_stats(area: dict) -> tuple:
    return RotationBroker.polygon_bbox(area.get("polygon", [[0, 0]]))


def _poly_stats_rotated(area: dict, rotation: int, W: float, H: float) -> tuple:
    poly = area.get("polygon", [[0, 0]])
    x0, x1, y0, y1 = RotationBroker.polygon_bbox_rotated(poly, rotation, W, H)
    return x0, x1, y0, y1
