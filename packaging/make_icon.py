"""Draw the launcher tile: a remote control sending infrared, on a dark rounded square
(not a house, which reads as a home-page button). Usage: make_icon.py <out.png>"""
import sys

from PIL import Image, ImageDraw

S = 100
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
dark = (24, 28, 36, 255)
d.rounded_rectangle((4, 4, S - 4, S - 4), radius=22, fill=dark)
teal = (40, 190, 170, 255)
d.rounded_rectangle((34, 34, 60, 88), radius=8, fill=teal)           # the remote, tilted look skipped for clarity
d.ellipse((42, 41, 52, 51), fill=(220, 60, 60, 255))                 # power button
for row in range(3):
    for col in range(2):
        x, y = 39 + col * 11, 57 + row * 9
        d.rounded_rectangle((x, y, x + 6, y + 5), radius=2, fill=dark)
for i, r in enumerate((10, 18, 26)):                                 # infrared waves from the top
    d.arc((47 - r, 30 - r, 47 + r, 30 + r), start=220, end=320, fill=(240, 140, 40, 255), width=4)
img.save(sys.argv[1])
