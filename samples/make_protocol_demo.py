"""Generate samples/protocol_demo.pdf + protocol_wash.pdf + protocol_table.pdf.

1-page Methods fixtures (v1 regression, wash/centrifuge mid-flow, stated
positions table). Stdlib only, hand-rolled minimal PDF (Helvetica, ASCII).
Run from repo root:
    .venv/bin/python samples/make_protocol_demo.py
"""

from pathlib import Path

LINES = [
    "Methods",
    "",
    "Seed cells in control, drug A, and drug B wells, 5 replicates each.",
    "",
    "Step 1: add 100 uL media to all wells; pipette slowly down the wall.",
    "Step 2: add vehicle to control wells.",
    "Step 3: add drug A to drug A wells; do not mix up and down after adding.",
    "Step 4: add drug B to drug B wells.",
    "Step 5: incubate 30 min.",
]

LINES_WASH = [
    "Methods",
    "",
    "Seed cells in treated and control wells, 4 replicates each.",
    "",
    "Step 1: add 50 uL compound to treated wells.",
    "Step 2: wash twice with 200 uL PBS.",
    "Step 3: centrifuge 5 min.",
    "Step 4: add 100 uL media to all wells.",
]

LINES_TABLE = [
    "Methods",
    "",
    "Plate map: control wells are B2, B3, B4, B5, B6.",
    "Drug A wells are C2, C3, C4, C5, C6.",
    "Seed 5 replicates per group.",
    "",
    "Step 1: add 100 uL media to every used well.",
    "Step 2: add 10 uL vehicle to control wells.",
    "Step 3: add 10 uL Drug A to Drug A wells.",
]


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build(lines: list[str]) -> bytes:
    text = "BT /F1 12 Tf 72 740 Td 16 TL " + " T* ".join(f"({_esc(l)}) Tj" for l in lines) + " ET"
    content = text.encode("ascii")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for o in offsets:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def main() -> None:
    here = Path(__file__).resolve().parent
    for name, lines in (("protocol_demo.pdf", LINES),
                        ("protocol_wash.pdf", LINES_WASH),
                        ("protocol_table.pdf", LINES_TABLE)):
        dest = here / name
        dest.write_bytes(build(lines))
        print(f"wrote {dest} ({dest.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
