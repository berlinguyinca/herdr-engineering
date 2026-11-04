"""Resource-limited PDF inspection and DOCX conversion adapters."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


class DocumentTools:
    def __init__(self, *, pdfinfo: str = "pdfinfo", libreoffice: str = "libreoffice",
                 timeout_seconds: float = 30):
        self.pdfinfo = pdfinfo
        self.libreoffice = libreoffice
        self.timeout_seconds = timeout_seconds

    def pdf_metadata(self, source: str | Path) -> dict:
        if shutil.which(self.pdfinfo) is None:
            return {"status": "unsupported", "reason": "pdfinfo_unavailable"}
        try:
            result = subprocess.run([self.pdfinfo, str(source)], text=True,
                                    capture_output=True, timeout=self.timeout_seconds,
                                    check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "failed", "reason": "pdfinfo_error"}
        if result.returncode != 0:
            return {"status": "failed", "reason": "invalid_pdf"}
        pages = re.search(r"^Pages:\s*(\d+)$", result.stdout, re.MULTILINE)
        return {"status": "passed", "pages": int(pages.group(1)) if pages else None}

    def docx_to_pdf(self, source: str | Path, output_dir: str | Path) -> dict:
        if shutil.which(self.libreoffice) is None:
            return {"status": "unsupported", "reason": "libreoffice_unavailable"}
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                [self.libreoffice, "--headless", "--norestore", "--convert-to", "pdf",
                 "--outdir", str(output), str(source)],
                text=True, capture_output=True, timeout=self.timeout_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "failed", "reason": "libreoffice_error"}
        if result.returncode != 0:
            return {"status": "failed", "reason": "conversion_failed"}
        rendered = output / (Path(source).stem + ".pdf")
        return {"status": "passed", "path": str(rendered)} if rendered.is_file() else {
            "status": "failed", "reason": "conversion_output_missing"}

