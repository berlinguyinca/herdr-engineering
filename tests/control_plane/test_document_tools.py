from herdr_engineering.control_plane.artifact_document_tools import DocumentTools


def test_pdf_metadata_reports_structured_unsupported_when_tool_is_missing(tmp_path):
    tools = DocumentTools(pdfinfo="/missing/pdfinfo", libreoffice="/missing/libreoffice")
    result = tools.pdf_metadata(tmp_path / "file.pdf")
    assert result["status"] == "unsupported"
    assert result["reason"] == "pdfinfo_unavailable"


def test_docx_conversion_reports_structured_unsupported_when_tool_is_missing(tmp_path):
    tools = DocumentTools(pdfinfo="/missing/pdfinfo", libreoffice="/missing/libreoffice")
    result = tools.docx_to_pdf(tmp_path / "file.docx", tmp_path / "out")
    assert result["status"] == "unsupported"
    assert result["reason"] == "libreoffice_unavailable"

