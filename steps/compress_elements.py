#!/usr/bin/env python3
"""
Create elements-compressed/ next to elements/, converting PNGs to JPEG
at quality 75 with max dimension 1500px. Run before step8 to reduce EPUB size.

Usage:
    python compress_elements.py '/path/to/book/elements/'
    python compress_elements.py '/path/to/book/elements/' --quality 80 --max-dim 1200
"""

import argparse
from pathlib import Path

from PIL import Image
from tqdm import tqdm


def compress(src_dir: Path, dst_dir: Path, quality: int, max_dim: int) -> None:
    dst_dir.mkdir(exist_ok=True)
    pngs = sorted(src_dir.glob("*.png"))
    if not pngs:
        print(f"No PNG files found in {src_dir}")
        return

    total_before = total_after = 0

    for src in tqdm(pngs, desc="Compressing", unit="img"):
        img = Image.open(src).convert("RGB")

        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        dst = dst_dir / (src.stem + ".jpg")
        img.save(dst, "JPEG", quality=quality, optimize=True, progressive=True)

        total_before += src.stat().st_size
        total_after += dst.stat().st_size

    print(f"Done: {total_before/1024/1024:.1f} MB → {total_after/1024/1024:.1f} MB "
          f"({100*(1 - total_after/total_before):.0f}% reduction)")
    print(f"Output: {dst_dir}")


def main():
    parser = argparse.ArgumentParser(description="Compress elements/ images to JPEG for EPUB.")
    parser.add_argument("elements_dir", help="Path to elements/ directory")
    parser.add_argument("--quality", type=int, default=75,
                        help="JPEG quality 1–95 (default: 75)")
    parser.add_argument("--max-dim", type=int, default=1500,
                        help="Max image dimension in pixels (default: 1500)")
    args = parser.parse_args()

    src_dir = Path(args.elements_dir)
    if not src_dir.is_dir():
        print(f"Not a directory: {src_dir}")
        raise SystemExit(1)

    dst_dir = src_dir.parent / (src_dir.name + "-compressed")
    compress(src_dir, dst_dir, args.quality, args.max_dim)


if __name__ == "__main__":
    main()
