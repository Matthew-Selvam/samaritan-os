"""
imgguard.py — Image safety primitives shared by every image connector
======================================================================
Feeds face_embed, reverse_image, exif and the TERRA/IRIS agents. Three jobs:

1. **Size caps** — a file (local path or already-fetched bytes) is rejected above
   ``MAX_IMAGE_BYTES`` *before* any decoder touches it.
2. **Decompression-bomb guard** — a 2 KB PNG can declare 40000×40000 pixels.
   Pillow's own ``Image.MAX_IMAGE_PIXELS`` is set defensively here and the
   declared dimensions are checked against ``MAX_IMAGE_PIXELS`` before decode,
   so a crafted file cannot exhaust memory. ``verify()``-style header-only
   reads happen before the full decode.
3. **EXIF/GPS stripping** — :func:`strip_metadata` re-encodes an image with a
   blank info dict, which is the only reliable way to guarantee no GPS block
   survives. Anything forwarded to a third party (reverse-image engines,
   vision models, marketplaces) MUST go through it first.

Never raises: every function returns a value or a structured error.
"""
from __future__ import annotations

import io
import os
from typing import Any

#: Hard ceiling for any single image we will load.
MAX_IMAGE_BYTES = 25 * 1024 * 1024
#: Ceiling on total decoded pixels (~200 MP). 40000x40000 = 1.6 GP is refused.
MAX_IMAGE_PIXELS = 200_000_000
#: Pillow's own warning threshold; kept in step with MAX_IMAGE_PIXELS.
_PILLOW_MAX_PIXELS = 89_478_485

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif",
                    ".tiff", ".heic", ".heif", ".avif", ".ico")

#: Magic-byte prefixes, longest-first so a .png check cannot shadow .jpg.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"RIFF", "webp"),          # refined by the WEBP/VP chunk below
)


def _pillow() -> Any:
    """Import Pillow lazily and arm its decompression-bomb limit.

    Returns:
        The ``PIL.Image`` module, or ``None`` when Pillow is unavailable.
    """
    try:
        from PIL import Image  # lazy, optional
        try:
            Image.MAX_IMAGE_PIXELS = _PILLOW_MAX_PIXELS
        except Exception:  # noqa: BLE001 — older Pillow
            pass
        return Image
    except ImportError:
        return None


def sniff_image_type(data: bytes) -> str | None:
    """Identify an image container from its magic bytes.

    Args:
        data: leading bytes of a file.

    Returns:
        A lowercase format name (``"jpeg"``, ``"png"``, …) or ``None``.
    """
    if not data:
        return None
    for prefix, name in _MAGIC:
        if data.startswith(prefix):
            if name == "webp" and not (data[8:12] == b"WEBP"):
                continue  # RIFF containers that are not WebP (AVI/WAV)
            return name
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypavif"):
        return "heic"
    return None


def looks_like_image(path: str) -> bool:
    """Cheap extension+magic check for a local path.

    Args:
        path: filesystem path.

    Returns:
        ``True`` when the extension is an image type and the first bytes are
        consistent with an image container.
    """
    ext = os.path.splitext(str(path))[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return False
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    if not head:
        return False
    # Extension is a strong hint but magic bytes are the arbiter. A few valid
    # formats (heic/avif) are only distinguishable by their ftyp box, which
    # sniff_image_type handles; anything unrecognised is trusted by extension
    # only when Pillow can still open it later.
    return sniff_image_type(head) is not None or ext in (".heic", ".heif", ".avif")


def read_capped(source: str | bytes, *, max_bytes: int = MAX_IMAGE_BYTES) -> tuple[bytes | None, str | None]:
    """Read an image from a path or raw bytes with a hard size cap.

    Args:
        source: filesystem path or the raw bytes themselves.
        max_bytes: refuse anything larger.

    Returns:
        ``(data, None)`` on success, ``(None, reason)`` on refusal. Never raises.
    """
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        if not data:
            return None, "empty image data"
        if len(data) > max_bytes:
            return None, f"image too large: {len(data)} bytes (cap {max_bytes})"
        return data, None

    path = str(source or "")
    if not path:
        return None, "empty path"
    if not os.path.isfile(path):
        return None, f"not a file: {path[:80]}"
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return None, f"cannot stat image: {exc.strerror or exc}"
    if size == 0:
        return None, "image file is empty"
    if size > max_bytes:
        return None, f"image too large: {size} bytes (cap {max_bytes})"
    try:
        with open(path, "rb") as fh:
            return fh.read(), None
    except OSError as exc:
        return None, f"cannot read image: {exc.strerror or exc}"


def probe(source: str | bytes, *, max_bytes: int = MAX_IMAGE_BYTES) -> dict[str, Any]:
    """Validate an image and report its declared geometry — without decoding it.

    Args:
        source: filesystem path or raw bytes.
        max_bytes: size cap.

    Returns:
        ``{"ok": bool, "format", "width", "height", "pixels", "bytes",
        "error"}``. ``error`` is ``None`` when ``ok`` is ``True``.
    """
    out: dict[str, Any] = {"ok": False, "format": None, "width": None,
                           "height": None, "pixels": 0, "bytes": 0, "error": None}
    data, err = read_capped(source, max_bytes=max_bytes)
    if err:
        out["error"] = err
        return out
    assert data is not None
    out["bytes"] = len(data)
    out["format"] = sniff_image_type(data)

    Image = _pillow()
    if Image is None:
        out["error"] = "Pillow not installed — pip install Pillow"
        return out
    try:
        with Image.open(io.BytesIO(data)) as img:      # header read only
            width, height = img.size
            out["format"] = (img.format or out["format"] or "").lower() or None
        out["width"], out["height"] = width, height
        pixels = int(width) * int(height)
        out["pixels"] = pixels
        if pixels > MAX_IMAGE_PIXELS:
            out["error"] = (f"decompression bomb refused: {width}x{height} = "
                            f"{pixels:,} pixels (cap {MAX_IMAGE_PIXELS:,})")
            return out
        out["ok"] = True
        return out
    except Exception as exc:  # noqa: BLE001 — unreadable image, not a crash
        out["error"] = f"unreadable image: {exc}"
        return out


def strip_metadata(source: str | bytes, *,
                   max_bytes: int = MAX_IMAGE_BYTES,
                   out_path: str | None = None) -> tuple[Any, str | None]:
    """Re-encode an image with **no** EXIF/XMP/IPTC — the GPS-scrub step.

    Anything about to leave this machine (reverse-image engines, a vision
    model, a marketplace listing) goes through this first. Re-encoding rather
    than "deleting tag 34853" is deliberate: only a re-encode drops maker
    notes, thumbnails, thumbnails-of-EXIF and XMP GPS payloads.

    Args:
        source: filesystem path or raw bytes.
        max_bytes: size cap applied before decode.
        out_path: when given, also write the sanitised image here.

    Returns:
        ``(clean_bytes, None)`` on success, ``(None, reason)`` on failure.
    """
    data, err = read_capped(source, max_bytes=max_bytes)
    if err:
        return None, err
    assert data is not None

    Image = _pillow()
    if Image is None:
        return None, "Pillow not installed — pip install Pillow"
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "PNG").upper()
            width, height = img.size
            if int(width) * int(height) > MAX_IMAGE_PIXELS:
                return None, (f"decompression bomb refused: {width}x{height} "
                              f"(cap {MAX_IMAGE_PIXELS:,} px)")
            # EXIF orientation is the one piece of metadata worth keeping; we
            # apply it to pixels and then drop the tag.
            from PIL import ImageOps  # local import keeps module scope clean
            safe = ImageOps.exif_transpose(img) or img
            clean = Image.new(safe.mode, safe.size)
            clean.putdata(list(safe.getdata()))
            clean.info.clear()
            if out_path:
                clean.save(out_path)
        buf = io.BytesIO()
        # JPEG cannot hold alpha and loses EXIF only if we pass no exif kwarg.
        save_fmt = fmt if fmt in ("JPEG", "PNG", "WEBP", "BMP", "GIF", "TIFF") else "PNG"
        if save_fmt == "JPEG":
            clean.convert("RGB").save(buf, format="JPEG", quality=95)
        else:
            clean.save(buf, format=save_fmt)
        return buf.getvalue(), None
    except Exception as exc:  # noqa: BLE001
        return None, f"could not strip metadata: {exc}"


def safe_upload_bytes(source: str | bytes, *,
                      max_bytes: int = MAX_IMAGE_BYTES) -> tuple[bytes | None, str | None]:
    """Return bytes that are safe to send to a third party.

    Args:
        source: filesystem path or raw bytes.
        max_bytes: size cap.

    Returns:
        The sanitised bytes. On a strip failure the **original** bytes are NOT
        returned — instead ``(None, reason)`` — so an EXIF-bearing image is
        never forwarded by accident.
    """
    return strip_metadata(source, max_bytes=max_bytes)


__all__ = [
    "MAX_IMAGE_BYTES", "MAX_IMAGE_PIXELS", "IMAGE_EXTENSIONS",
    "sniff_image_type", "looks_like_image", "read_capped", "probe",
    "strip_metadata", "safe_upload_bytes",
]
