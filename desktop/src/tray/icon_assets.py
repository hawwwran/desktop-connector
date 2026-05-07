"""Sparkle-star tray icon compositing.

Binary connected/disconnected is carried by SHAPE (filled vs outline
star) so it's still readable when the tray theme forces monochrome.
Sub-state (uploading, reconnecting, remote offline) is carried by TINT.

Composited once at module import; ``_make_icon`` returns a copy and is
safe from any thread. ``_bake_state_paths`` writes each state to a
stable path so AppIndicator backends can swap by path without the
delete + mktemp churn that produced the ~500 ms default-icon flash.
"""

from pathlib import Path

from PIL import Image, ImageDraw

from ..brand import (
    DC_BLUE_400_RGB,
    DC_BLUE_800_RGB,
    DC_ORANGE_700_RGB,
    DC_YELLOW_500_RGB,
)


_ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
_BRAND_DIR = _ASSETS_DIR / "brand"

# Tray icons are rendered by pystray at 22–48 px (64 px on HiDPI). The
# PIL image is PNG-encoded on every swap and written to a temp file the
# indicator re-reads; at 600 px that's ~35 ms, long enough for the
# indicator to fall back to the default app icon while reloading.
# 128 px encodes in ~3 ms, below one frame, so the swap looks instant.
_TRAY_RENDER_SIZE = 128


def _load_master(name: str) -> Image.Image | None:
    p = _BRAND_DIR / name
    if not p.exists():
        return None
    img = Image.open(p).convert("RGBA")
    img.load()
    return img


def _tint(mask: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
    """Recolor a black-on-transparent mask to `rgb`, preserving alpha."""
    alpha = mask.split()[-1]
    tinted = Image.new("RGBA", mask.size, (*rgb, 255))
    tinted.putalpha(alpha)
    return tinted


def _crop_and_pad(img: Image.Image, bbox: tuple[int, int, int, int],
                  pad_ratio: float = 0.02) -> Image.Image:
    """Crop `img` to `bbox`, then center it on a padded square canvas.
    All masks sharing the same `bbox` + `pad_ratio` stay pixel-aligned."""
    cropped = img.crop(bbox)
    w, h = cropped.size
    side = max(w, h)
    pad = max(1, int(side * pad_ratio))
    canvas = side + 2 * pad
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    out.alpha_composite(cropped, (pad + (side - w) // 2, pad + (side - h) // 2))
    return out


def _load_icons() -> dict[str, Image.Image]:
    """Build one PIL image per tray state. Composited once at import time.
    Keys match ``_current_state_key`` in status.py."""
    full_raw = _load_master("star-full-bw.png")
    center_raw = _load_master("star-center-bw.png")
    # Anchor center to the full-star bbox so the inner diamond stays
    # geometrically centered inside the outer star after trimming.
    if full_raw is not None:
        star_bbox = full_raw.split()[-1].getbbox()
        full = _crop_and_pad(full_raw, star_bbox)
        center = _crop_and_pad(center_raw, star_bbox) if center_raw is not None else None
    else:
        full = center = None

    icons: dict[str, Image.Image] = {}

    if full is not None:
        # Shape is always a filled star; color scale carries state:
        #   dark blue  = fully connected (server + phone)
        #   sky blue   = half-connected  (server ok, phone offline)
        #   yellow     = reconnecting    (server handshake in progress)
        #   orange     = offline         (server unreachable)
        icons["connected"] = _tint(full, DC_BLUE_800_RGB)
        icons["remote_offline"] = _tint(full, DC_BLUE_400_RGB)
        icons["reconnecting"] = _tint(full, DC_YELLOW_500_RGB)
        icons["disconnected"] = _tint(full, DC_ORANGE_700_RGB)

        if center is not None:
            upload = _tint(full, DC_BLUE_800_RGB)
            inner = _tint(center, DC_YELLOW_500_RGB)
            upload.alpha_composite(inner)
            icons["uploading"] = upload
        else:
            icons["uploading"] = _tint(full, DC_YELLOW_500_RGB)

    if not icons:
        # Installer didn't ship brand/, or running from an old checkout.
        # Draw flat-colored circles as a bare fallback so the tray still works.
        size = _TRAY_RENDER_SIZE
        for state, rgb in (("connected", DC_BLUE_800_RGB),
                           ("remote_offline", DC_BLUE_400_RGB),
                           ("uploading", DC_YELLOW_500_RGB),
                           ("reconnecting", DC_YELLOW_500_RGB),
                           ("disconnected", DC_ORANGE_700_RGB)):
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            ImageDraw.Draw(img).ellipse([16, 16, size - 16, size - 16], fill=rgb)
            icons[state] = img

    # Downsample once at load time so icon swaps don't re-encode 600 px PNGs.
    for key, img in icons.items():
        if img.size != (_TRAY_RENDER_SIZE, _TRAY_RENDER_SIZE):
            icons[key] = img.resize(
                (_TRAY_RENDER_SIZE, _TRAY_RENDER_SIZE), Image.LANCZOS)

    return icons


_icons = _load_icons()


def _make_icon(state: str) -> Image.Image:
    """Return a copy of a pre-composited state icon. Safe from any thread."""
    img = _icons.get(state) or _icons.get("connected") or next(iter(_icons.values()))
    return img.copy()


def _bake_state_paths(cache_dir: Path) -> dict[str, str]:
    """Write each state icon to a stable file in `cache_dir` once. Returns
    {state: absolute_path}.

    pystray's default _update_icon deletes the current temp file, mktemps a
    new random path, then calls AppIndicator.set_icon(new_path). The delete +
    rename forces a theme/path lookup in the tray frontend (GNOME Shell's
    AppIndicator ext, KDE systray, xfce4-indicator-plugin), which briefly
    renders the stock application icon until the new path resolves — that's
    the ~500ms "burger" flash. Writing each state once to a stable path
    lets us call AppIndicator.set_icon(same_path) without file churn, so
    the frontend sees only a pixbuf-reload and skips the fallback render.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for state, img in _icons.items():
        p = cache_dir / f"tray-{state}.png"
        img.save(p, format="PNG")
        paths[state] = str(p)
    return paths
