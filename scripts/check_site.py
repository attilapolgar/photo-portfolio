#!/usr/bin/env python3
"""Check the built site for the mistakes that have actually reached it.

Every check here corresponds to a defect that shipped at least once, or to an
invariant the build depends on. Run locally with:

    python3 scripts/check_site.py

Needs Pillow (image dimensions and EXIF); nothing else.
"""

import re
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"
IMAGES = ROOT / "images"

# EXIF tags that identify the camera body or lens. They serve no purpose on a
# public site and tie every upload back to one device. Both live in the Exif
# sub-IFD, not in IFD0 — reading only the top level finds nothing and passes.
SERIAL_TAGS = {0xA431: "BodySerialNumber", 0xA435: "LensSerialNumber"}
EXIF_IFD = 0x8769
GPS_IFD = 0x8825

failures: list[str] = []
notes: list[str] = []


def fail(check: str, detail: str) -> None:
    failures.append(f"{check}: {detail}")


def check_single_title(html: str) -> None:
    """A stray <title> shipped once: the generator lifted the source page's own
    title into <head> beside the authored one, and the tail leaked as text."""
    n = len(re.findall(r"<title>", html))
    if n != 1:
        fail("title", f"expected exactly 1 <title>, found {n}")


def check_no_nested_comments(html: str) -> None:
    """HTML comments do not nest. A comment quoting `<!--DEV-->` inside itself
    terminated early and rendered its tail as visible text on the page."""
    for m in re.finditer(r"<!--.*?-->", html, re.S):
        if "<!--" in m.group(0)[4:]:
            fail("comments", f"nested comment opener in {m.group(0)[:60]!r}")


def check_images_exist(html: str) -> set[str]:
    """Filenames are generated from the plate names, so a rename can silently
    orphan a file or point the page at one that no longer exists."""
    referenced = set(re.findall(r'(?:src|srcset)="([^"]*)"', html))
    files = set()
    for attr in referenced:
        for part in attr.split(","):
            path = part.strip().split(" ")[0]
            if path.startswith("images/"):
                files.add(path)
    for f in sorted(files):
        if not (ROOT / f).is_file():
            fail("images", f"{f} referenced but missing")

    on_disk = {f"images/{p.name}" for p in IMAGES.iterdir() if p.suffix == ".jpg"}
    orphans = on_disk - files
    if orphans:
        fail("images", f"unreferenced file(s) in images/: {', '.join(sorted(orphans))}")
    return files


def check_dimensions(html: str) -> None:
    """width/height reserve layout space before the image decodes. Stale values
    make the page jump as each plate paints in."""
    for src, w, h in re.findall(
        r'<img src="(images/[^"]+)"[^>]*width="(\d+)" height="(\d+)"', html
    ):
        p = ROOT / src
        if not p.is_file():
            continue
        with Image.open(p) as im:
            if (im.width, im.height) != (int(w), int(h)):
                fail("dimensions", f"{src} is {im.width}x{im.height}, markup says {w}x{h}")


def check_srcset(html: str) -> None:
    """Each plate ships a 900px variant; a phone downloads 852KB instead of
    4.8MB only while every img actually carries the candidate."""
    imgs = re.findall(r"<img [^>]*>", html)
    for tag in imgs:
        src = re.search(r'src="(images/[^"]+)"', tag)
        if not src:
            continue
        if "srcset=" not in tag:
            fail("srcset", f"{src.group(1)} has no srcset")
            continue
        if "sizes=" not in tag:
            fail("srcset", f"{src.group(1)} has srcset but no sizes")
        small = src.group(1).replace(".jpg", "-900.jpg")
        if small not in tag:
            fail("srcset", f"{src.group(1)} does not offer {small}")


def check_no_local_paths(html: str) -> None:
    """An earlier revision shipped absolute /Users/... paths in a data attribute."""
    for pat in (r"/Users/", r"DXOPhotoLab", r"open -a "):
        if re.search(pat, html):
            fail("leaks", f"{pat!r} appears in index.html")


def check_image_metadata(files: set[str]) -> None:
    """Serial numbers and GPS are stripped at build time by hand. Nothing
    enforces it, so a forgotten step would publish them."""
    for f in sorted(files):
        p = ROOT / f
        if not p.is_file():
            continue
        with Image.open(p) as im:
            exif = im.getexif()
            scopes = (exif, exif.get_ifd(EXIF_IFD))
            for tag, name in SERIAL_TAGS.items():
                for scope in scopes:
                    value = scope.get(tag)
                    if value not in (None, "", 0):
                        fail("metadata", f"{f} still carries {name}={value!r}")
            gps = exif.get_ifd(GPS_IFD)
            if gps:
                fail("metadata", f"{f} still carries GPS data ({len(gps)} tag(s))")


def check_readme_matches(html: str) -> None:
    """The readme duplicates the plate list and drifted from it twice, silently,
    within minutes of a merge."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    page = [
        (hu, la)
        for hu, la in zip(
            re.findall(r'<h2 class="hu">([^<]+)</h2>', html),
            re.findall(r'<p class="la">([^<]+)</p>', html),
        )
    ]
    rows = re.findall(r"^\| (\d+) \| ([^|]+?) \| \*([^*]+)\* \|$", readme, re.M)
    if len(rows) != len(page):
        fail("readme", f"table lists {len(rows)} plates, page has {len(page)}")
        return
    for (num, hu, la), (page_hu, page_la) in zip(rows, page):
        if hu.strip() != page_hu or la.strip() != page_la:
            fail(
                "readme",
                f"row {num} says {hu.strip()!r}/{la.strip()!r}, "
                f"page says {page_hu!r}/{page_la!r}",
            )


def main() -> int:
    if not INDEX.is_file():
        print("index.html not found", file=sys.stderr)
        return 1
    html = INDEX.read_text(encoding="utf-8")

    check_single_title(html)
    check_no_nested_comments(html)
    files = check_images_exist(html)
    check_dimensions(html)
    check_srcset(html)
    check_no_local_paths(html)
    check_image_metadata(files)
    check_readme_matches(html)

    plates = len(re.findall(r'<h2 class="hu">', html))
    notes.append(f"{plates} plates, {len(files)} image files referenced")

    for n in notes:
        print(f"  {n}")
    if failures:
        print(f"\n{len(failures)} problem(s):\n", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
