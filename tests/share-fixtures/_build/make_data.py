"""Regenerate the share-fixture data files: ``table.parquet`` (both parquet bundles) and ``pdf-only/report.pdf``.

Run from the repo root:  python3 tests/share-fixtures/_build/make_data.py
Needs pyarrow (any recent version; the committed file was written with pyarrow 24.0.0). Deterministic: no clock, no
randomness, so the bytes only change when pyarrow's writer does.

The parquet file is deliberately larger than a parquet reader's first footer read (hyparquet fetches ~512 KiB from the
end) and split into several row groups, so the pages provably load it with HTTP range requests instead of one GET.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent.parent
ROWS = 40_000
ROW_GROUP = 5_000


def write_parquet(dest: Path) -> None:
    ids = pa.array(range(ROWS), pa.int64())
    names = pa.array([f"sample-{i:06d}-{(i * 7919) % 100003:05d}" for i in range(ROWS)])
    mz = pa.array([100.0 + ((i * 2654435761) % 900_000) / 1000.0 for i in range(ROWS)], pa.float64())
    intensity = pa.array([(i * 40503) % 1_000_003 for i in range(ROWS)], pa.int64())
    table = pa.table({"id": ids, "name": names, "mz": mz, "intensity": intensity})
    dest.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, dest, row_group_size=ROW_GROUP, compression="snappy", use_dictionary=False)


def _pdf() -> bytes:
    """A one-page PDF with a visible headline, built by hand (no PDF library needed)."""
    text = b"BT /F1 28 Tf 72 700 Td (Share fixture PDF) Tj 0 -40 Td /F1 14 Tf (herdr artifact share-links) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> "
        b"/Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text), text),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def main() -> None:
    first = HERE / "html-parquet-hyparquet" / "data" / "table.parquet"
    write_parquet(first)
    second = HERE / "html-parquet-duckdb" / "data" / "table.parquet"
    second.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(first, second)
    (HERE / "pdf-only").mkdir(exist_ok=True)
    (HERE / "pdf-only" / "report.pdf").write_bytes(_pdf())
    print(first, first.stat().st_size, "bytes,", pq.ParquetFile(first).num_row_groups, "row groups")


if __name__ == "__main__":
    main()
