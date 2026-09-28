#!/usr/bin/env python3
"""Generate the macOS menu-bar status icons from the production app icon.

Produces four 256 px PNGs:
  - MenubarIcon.png        — full-color  (SSH connected — the icon body
                             is blue, i.e. the blue=SSH semantics)
  - MenubarIcon-green.png  — green       (VPN connected)
  - MenubarIcon-yellow.png — yellow      (connecting / paused)
  - MenubarIcon-gray.png   — grayscale   (disconnected)

menu_builder.py selects the appropriate one based on connection state.
"""
from PIL import Image

ICON_SOURCE = "icons/magic-ai-router-macos-v2.icns"
OUTPUT_COLOR = "assets/MenubarIcon.png"
OUTPUT_GREEN = "assets/MenubarIcon-green.png"
OUTPUT_YELLOW = "assets/MenubarIcon-yellow.png"
OUTPUT_GRAY = "assets/MenubarIcon-gray.png"
SIZE = 256


def _extract_square(img):
    """Crop to alpha bbox, center in a square."""
    bbox = img.getchannel("A").getbbox()
    cropped = img.crop(bbox)
    w, h = cropped.size
    side = max(w, h)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(cropped, ((side - w) // 2, (side - h) // 2))
    return square.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    img = Image.open(ICON_SOURCE).convert("RGBA")
    color = _extract_square(img)
    color.save(OUTPUT_COLOR)
    print(f"Created {OUTPUT_COLOR} ({SIZE}x{SIZE} color)")

    # 亮度基底：Pillow convert("L") 即 ITU-R 601-2 加权（0.299/0.587/
    # 0.114——与原 numpy 实现同权）。R9-C2 去 numpy：lock 无此包，带
    # numpy 的再生成路径曾是 ModuleNotFoundError 哑弹。
    gray = color.convert("L")
    alpha = color.getchannel("A")

    # Grayscale
    Image.merge("RGBA", (gray, gray, gray, alpha)).save(OUTPUT_GRAY)
    print(f"Created {OUTPUT_GRAY} ({SIZE}x{SIZE} grayscale)")

    # Green (VPN connected): luminance mapped into system-green tones
    # (systemGreen ≈ #34C759 → r 0.25 / g 1.0 / b 0.45 of luminance)
    Image.merge("RGBA", (
        gray.point(lambda v: int(v * 0.25)), gray,
        gray.point(lambda v: int(v * 0.45)), alpha)).save(OUTPUT_GREEN)
    print(f"Created {OUTPUT_GREEN} ({SIZE}x{SIZE} green)")

    # Yellow: map luminance into warm yellow tones (r 1.0 / g 0.78 / b 0.15)
    Image.merge("RGBA", (
        gray, gray.point(lambda v: int(v * 0.78)),
        gray.point(lambda v: int(v * 0.15)), alpha)).save(OUTPUT_YELLOW)
    print(f"Created {OUTPUT_YELLOW} ({SIZE}x{SIZE} yellow)")


if __name__ == "__main__":
    main()
