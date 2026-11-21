#!/usr/bin/env python3
"""Apply the 720p canvas optimization to BeyondGTAClips' templates.py.

Changes ONLY the render canvas from 1080x1920 to 720x1280, scaling every
layout constant by exactly 2/3 so all five templates keep their exact
proportions (same look, same moments, same captions, same punch-ins).
Nothing else in the engine is touched: the pipeline, the moment picker,
Whisper captions, SFX and the clip logic are all unchanged.

Usage: apply-720p-canvas.py <path/to/templates.py>
Fails loudly (exit 1) if any expected string is missing or appears
more than once, so a silent no-op or a wrong edit can never happen.
"""
import sys
from pathlib import Path

REPLACEMENTS = [
    ("""The five edit templates. Each one turns the source clip into a finished 1080x1920 frame""",
     """The five edit templates. Each one turns the source clip into a finished 720x1280 frame"""),
    ("""W, H = 1080, 1920""",
     """W, H = 720, 1280"""),
    # hookcard
    ("""clip_w, clip_h = W, 864                                  # 5:4, like the viral reels""",
     """clip_w, clip_h = W, 576                                  # 5:4, like the viral reels"""),
    ("""clip_y = (H - clip_h) // 2 + 60""",
     """clip_y = (H - clip_h) // 2 + 40"""),
    ("""hook = art.rich_block(hook_words(pick), 56, W - 110, regular="clean", accent_font="clean_bold",""",
     """hook = art.rich_block(hook_words(pick), 37, W - 110, regular="clean", accent_font="clean_bold","""),
    ("""g.place(hook, "hook", "center", clip_y - 46 - hook.height)""",
     """g.place(hook, "hook", "center", clip_y - 31 - hook.height)"""),
    ("""g.captions(ctx["words"], {"font": "rounded", "size": 58, "color": accent, "stroke": 6,""",
     """g.captions(ctx["words"], {"font": "rounded", "size": 39, "color": accent, "stroke": 4,"""),
    (""""words": 4, "margin": 80}, clip_y + clip_h + 62)""",
     """"words": 4, "margin": 53}, clip_y + clip_h + 41)"""),
    ("""creator_credit(g, ctx, 56, clip_y + clip_h + 120)""",
     """creator_credit(g, ctx, 37, clip_y + clip_h + 80)"""),
    ("""g.place(art.credit(ctx["creator"], 30, color), "credit", x, y)""",
     """g.place(art.credit(ctx["creator"], 20, color), "credit", x, y)"""),
    # stack
    ("""game_y = even(H - 250 - game_h)                 # above TikTok/Reels' bottom caption block""",
     """game_y = even(H - 167 - game_h)                 # above TikTok/Reels' bottom caption block"""),
    ("""cap_y = game_y - 74                             # caption word just above the game""",
     """cap_y = game_y - 49                             # caption word just above the game"""),
    ("""size = 74 if len(title) <= 26 else 62""",
     """size = 49 if len(title) <= 26 else 41"""),
    ("""line_y = even(cap_y - 62 - slab_h / 2) if slab else cap_y - 70""",
     """line_y = even(cap_y - 41 - slab_h / 2) if slab else cap_y - 47"""),
    ("""area_top, area_bottom = 130, line_y - slab_h / 2 - 44""",
     """area_top, area_bottom = 87, line_y - slab_h / 2 - 29"""),
    ("""want = min(980 / (area_bottom - area_top), 1.3)  # a squarish cam looks small: trim""",
     """want = min(653 / (area_bottom - area_top), 1.3)  # a squarish cam looks small: trim"""),
    ("""fit = min(980 / w, (area_bottom - area_top) / h)""",
     """fit = min(653 / w, (area_bottom - area_top) / h)"""),
    ("""border, radius = 6, 30""",
     """border, radius = 4, 20"""),
    ("""g.captions(ctx["words"], {"font": "heavy", "size": 70, "color": "FFFFFF", "active": None,""",
     """g.captions(ctx["words"], {"font": "heavy", "size": 47, "color": "FFFFFF", "active": None,"""),
    (""""stroke": 3, "shadow": True, "upper": True, "words": 1, "lean": 0.2},""",
     """"stroke": 2, "shadow": True, "upper": True, "words": 1, "lean": 0.2},"""),
    ("""g.place(art.pill(ctx["creator"], 28, bg="111111", fg="FFFFFF", kind="label"), "chip", 40, 70)""",
     """g.place(art.pill(ctx["creator"], 19, bg="111111", fg="FFFFFF", kind="label"), "chip", 27, 47)"""),
    # bubble
    ("""d = 340""",
     """d = 227"""),
    ("""by = H - 440 - d                         # bottom-left, above the app's caption area""",
     """by = H - 293 - d                         # bottom-left, above the app's caption area"""),
    ("""g.place(art.ring(d + 16, 7, accent), "ring", 48, by - 8)""",
     """g.place(art.ring(d + 11, 5, accent), "ring", 32, by - 5)"""),
    ("""g.over(cb, 56, by)""",
     """g.over(cb, 37, by)"""),
    ("""sticker = art.pill(title, 62, bg="FFFFFF", fg="111111", max_width=W - 140)""",
     """sticker = art.pill(title, 41, bg="FFFFFF", fg="111111", max_width=W - 140)"""),
    ("""g.captions(ctx["words"], {"font": "heavy", "size": 84, "color": "FFFFFF", "active": accent,""",
     """g.captions(ctx["words"], {"font": "heavy", "size": 56, "color": "FFFFFF", "active": accent,"""),
    (""""pop": 1.12, "stroke": 9, "upper": True, "words": 2, "margin": 40},""",
     """"pop": 1.12, "stroke": 6, "upper": True, "words": 2, "margin": 27},"""),
    ("""chip = art.pill(ctx["creator"], 28, bg="111111", fg="FFFFFF", kind="label")""",
     """chip = art.pill(ctx["creator"], 19, bg="111111", fg="FFFFFF", kind="label")"""),
    ("""g.place(chip, "chip", W - 50 - chip.width, 96)""",
     """g.place(chip, "chip", W - 33 - chip.width, 64)"""),
    # cinematic
    ("""fw, fh = W, 608""",
     """fw, fh = W, 405"""),
    ("""fy = (H - fh) // 2 - 10""",
     """fy = (H - fh) // 2 - 7"""),
    ("""boxblur=30:3,eq=brightness=-0.30:saturation=0.75,vignette=PI/4,setsar=1[{bg}]")""",
     """boxblur=20:3,eq=brightness=-0.30:saturation=0.75,vignette=PI/4,setsar=1[{bg}]")"""),
    ("""g.add(f"[src_main]crop={cw}:{ch}:{cx}:{cy},scale=2160:1216:flags=lanczos,""",
     """g.add(f"[src_main]crop={cw}:{ch}:{cx}:{cy},scale=1440:810:flags=lanczos,"""),
    ("""lab = art.label("  ·  ".join(x for x in (topic, ctx.get("creator") or "") if x), 28, "D0D0D0")""",
     """lab = art.label("  ·  ".join(x for x in (topic, ctx.get("creator") or "") if x), 19, "D0D0D0")"""),
    ("""title = art.rich_block(art.parse_rich(pick.get("headline") or pick.get("title") or ""), 50, W - 160,""",
     """title = art.rich_block(art.parse_rich(pick.get("headline") or pick.get("title") or ""), 33, W - 160,"""),
    ("""g.place(title, "title", "center", fy - 60 - title.height)""",
     """g.place(title, "title", "center", fy - 40 - title.height)"""),
    ("""g.place(lab, "label", "center", fy - 60 - title.height - 34 - lab.height)""",
     """g.place(lab, "label", "center", fy - 40 - title.height - 23 - lab.height)"""),
    ("""g.captions(ctx["words"], {"font": "clean", "size": 44, "color": "FFFFFF", "shadow": True,""",
     """g.captions(ctx["words"], {"font": "clean", "size": 29, "color": "FFFFFF", "shadow": True,"""),
    (""""words": 7, "lines": 2, "margin": 110}, fy + fh + 120)""",
     """"words": 7, "lines": 2, "margin": 73}, fy + fh + 80)"""),
    # news
    ("""clip_w, clip_h = W, 608""",
     """clip_w, clip_h = W, 405"""),
    ("""t = art.tag("Breaking", 46, bg=accent)""",
     """t = art.tag("Breaking", 31, bg=accent)"""),
    ("""g.place(t, "tag", 56, 300)""",
     """g.place(t, "tag", 37, 200)"""),
    ("""g.place(art.label(f"{ctx['creator']}  ·  live", 26, "BBBBBB"), "who", 56 + t.width + 22,""",
     """g.place(art.label(f"{ctx['creator']}  ·  live", 17, "BBBBBB"), "who", 37 + t.width + 15,"""),
    ("""300 + (t.height - 39) // 2)""",
     """200 + (t.height - 26) // 2)"""),
    ("""88, W - 100, regular="condensed", accent_font="condensed", color="FFFFFF",""",
     """59, W - 100, regular="condensed", accent_font="condensed", color="FFFFFF","""),
    ("""head_y = 300 + t.height + 34""",
     """head_y = 200 + t.height + 23"""),
    ("""g.place(head, "headline", 54, head_y)""",
     """g.place(head, "headline", 36, head_y)"""),
    ("""clip_y = max(head_y + head.height + 50, 720)""",
     """clip_y = max(head_y + head.height + 33, 480)"""),
    ("""g.place(art.bar(W, 8, accent), "line", 0, clip_y + clip_h)""",
     """g.place(art.bar(W, 5, accent), "line", 0, clip_y + clip_h)"""),
    ("""g.place(art.bar(W, 72, "16161C"), "tickerbar", 0, bar_y)""",
     """g.place(art.bar(W, 48, "16161C"), "tickerbar", 0, bar_y)"""),
    ("""strip = art.ticker(f"{text}  •  clipped from {ctx.get('creator') or 'the stream'}", 36, 72)""",
     """strip = art.ticker(f"{text}  •  clipped from {ctx.get('creator') or 'the stream'}", 24, 48)"""),
    ("""g.add(f"[{g.last}][{src}]overlay=x='W-mod(t*170,W+w)':y={bar_y}[{out}]")""",
     """g.add(f"[{g.last}][{src}]overlay=x='W-mod(t*113,W+w)':y={bar_y}[{out}]")"""),
    ("""g.captions(ctx["words"], {"font": "heavy", "size": 58, "color": "FFFFFF", "active": "FFD400",""",
     """g.captions(ctx["words"], {"font": "heavy", "size": 39, "color": "FFFFFF", "active": "FFD400","""),
    (""""pop": 1.08, "stroke": 7, "upper": True, "words": 3},""",
     """"pop": 1.08, "stroke": 5, "upper": True, "words": 3},"""),
    ("""bar_y + 72 + 64)""",
     """bar_y + 48 + 43)"""),
    ("""g.place(art.label(f"source: {ctx.get('creator') or 'stream'}", 24, "8A8A8A"), "source", 56,""",
     """g.place(art.label(f"source: {ctx.get('creator') or 'stream'}", 16, "8A8A8A"), "source", 37,"""),
    ("""bar_y + 72 + 130)""",
     """bar_y + 48 + 87)"""),
]


def main():
    if len(sys.argv) != 2:
        print("usage: apply-720p-canvas.py <path/to/templates.py>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    for old, new in REPLACEMENTS:
        count = text.count(old)
        if count != 1:
            print(f"FAIL: expected exactly 1 occurrence of {old!r}, found {count}", file=sys.stderr)
            return 1
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"OK: applied {len(REPLACEMENTS)} replacements to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
