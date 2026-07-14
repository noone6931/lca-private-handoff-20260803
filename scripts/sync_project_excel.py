from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "project-management.md"
OUTPUT = ROOT / "docs" / "local-coding-agent-project-management.xlsx"


@dataclass(frozen=True)
class SheetData:
    name: str
    rows: list[list[str]]


def main() -> int:
    sheets = parse_markdown_tables(SOURCE.read_text(encoding="utf-8"))
    if not sheets:
        raise SystemExit(f"No tables found in {SOURCE}")
    write_xlsx(OUTPUT, sheets)
    print(f"Wrote {OUTPUT.relative_to(ROOT)} from {SOURCE.relative_to(ROOT)}")
    return 0


def parse_markdown_tables(markdown: str) -> list[SheetData]:
    sheets: list[SheetData] = []
    current_name: str | None = None
    table_lines: list[str] = []

    def flush() -> None:
        nonlocal table_lines
        if current_name and table_lines:
            rows = parse_table(table_lines)
            if rows:
                sheets.append(SheetData(current_name, rows))
        table_lines = []

    for line in markdown.splitlines():
        if line.startswith("## "):
            flush()
            current_name = line.removeprefix("## ").strip()
            continue
        if current_name and line.lstrip().startswith("|"):
            table_lines.append(line)
        elif table_lines and not line.strip():
            # Markdown authors often add visual spacing inside one long table.
            # Keep the current table open until content or a new section ends it.
            continue
        elif table_lines:
            flush()
    flush()
    return sheets


def parse_table(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    expected_width: int | None = None
    for line_number, line in enumerate(lines, start=1):
        cells = [cell.strip().replace("<br>", "\n") for cell in split_markdown_row(line)]
        if expected_width is None:
            expected_width = len(cells)
        elif len(cells) != expected_width:
            raise ValueError(
                f"Markdown table row {line_number} has {len(cells)} columns; "
                f"expected {expected_width}: {line}"
            )
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        rows.append(cells)
    return rows


def split_markdown_row(line: str) -> list[str]:
    """Split a Markdown table row without treating code-span pipes as columns."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith(r"\|"):
        text = text[:-1]

    cells: list[str] = []
    cell: list[str] = []
    code_ticks = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] == "|":
            cell.append("|")
            index += 2
            continue
        if char == "`":
            end = index + 1
            while end < len(text) and text[end] == "`":
                end += 1
            tick_count = end - index
            if code_ticks == 0:
                code_ticks = tick_count
            elif code_ticks == tick_count:
                code_ticks = 0
            cell.append(text[index:end])
            index = end
            continue
        if char == "|" and code_ticks == 0:
            cells.append("".join(cell))
            cell = []
        else:
            cell.append(char)
        index += 1
    cells.append("".join(cell))
    return cells


def write_xlsx(path: Path, sheets: list[SheetData]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml(len(sheets)))
        archive.writestr("_rels/.rels", root_rels_xml())
        archive.writestr("docProps/core.xml", core_xml())
        archive.writestr("docProps/app.xml", app_xml(sheets))
        archive.writestr("xl/workbook.xml", workbook_xml(sheets))
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml(len(sheets)))
        archive.writestr("xl/styles.xml", styles_xml())
        for index, sheet in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", worksheet_xml(sheet))


def worksheet_xml(sheet: SheetData) -> str:
    table = sheet.rows
    width = max(len(row) for row in table)
    last_col = column_name(width)
    last_row = len(table) + 1
    col_widths = infer_column_widths(table)
    cols_xml = "".join(
        f'<col min="{idx}" max="{idx}" width="{width_value}" customWidth="1"/>'
        for idx, width_value in enumerate(col_widths, start=1)
    )

    data_rows = [row_xml(1, [sheet.name], width, style=1)]
    for row_index, row in enumerate(table, start=2):
        data_rows.append(row_xml(row_index, row, width, style=2 if row_index == 2 else 0))

    merge_xml = f'<mergeCells count="1"><mergeCell ref="A1:{last_col}1"/></mergeCells>'
    return xml_doc(
        f"""
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
           xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="A1:{last_col}{last_row}"/>
  <sheetViews>
    <sheetView workbookViewId="0">
      <pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/>
    </sheetView>
  </sheetViews>
  <cols>{cols_xml}</cols>
  <sheetData>{''.join(data_rows)}</sheetData>
  {merge_xml}
</worksheet>
"""
    )


def row_xml(row_index: int, row: list[str], width: int, *, style: int) -> str:
    cells = []
    padded = row + [""] * (width - len(row))
    for col_index, value in enumerate(padded, start=1):
        ref = f"{column_name(col_index)}{row_index}"
        cells.append(cell_xml(ref, value, style=style))
    return f'<row r="{row_index}">{"".join(cells)}</row>'


def cell_xml(ref: str, value: str, *, style: int) -> str:
    if value == "":
        return f'<c r="{ref}" s="{style}"/>'
    numeric = numeric_value(value)
    if numeric is not None:
        return f'<c r="{ref}" s="{style}"><v>{numeric}</v></c>'
    text = escape(value, quote=False)
    preserve = ' xml:space="preserve"' if value.strip() != value else ""
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t{preserve}>{text}</t></is></c>'


def numeric_value(value: str) -> str | None:
    stripped = value.strip()
    if re.fullmatch(r"-?\d+", stripped):
        return stripped
    if re.fullmatch(r"-?\d+\.\d+", stripped):
        return stripped
    return None


def infer_column_widths(rows: list[list[str]]) -> list[int]:
    width = max(len(row) for row in rows)
    widths: list[int] = []
    for col_index in range(width):
        max_len = 0
        for row in rows:
            if col_index >= len(row):
                continue
            max_len = max(max_len, max((len(part) for part in row[col_index].splitlines()), default=0))
        widths.append(min(max(max_len + 2, 12), 48))
    return widths


def column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def workbook_xml(sheets: list[SheetData]) -> str:
    sheet_entries = []
    for index, sheet in enumerate(sheets, start=1):
        sheet_entries.append(
            f'<sheet name="{escape(sheet.name)}" sheetId="{index}" r:id="rId{index}"/>'
        )
    return xml_doc(
        f"""
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>{''.join(sheet_entries)}</sheets>
</workbook>
"""
    )


def workbook_rels_xml(sheet_count: int) -> str:
    rels = []
    for index in range(1, sheet_count + 1):
        rels.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
    rels.append(
        f'<Relationship Id="rId{sheet_count + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    return xml_doc(f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{"".join(rels)}</Relationships>')


def content_types_xml(sheet_count: int) -> str:
    sheet_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return xml_doc(
        f"""
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  {sheet_overrides}
</Types>
"""
    )


def root_rels_xml() -> str:
    return xml_doc(
        """
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""
    )


def styles_xml() -> str:
    return xml_doc(
        """
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="3">
    <font><sz val="11"/><name val="Arial"/></font>
    <font><b/><sz val="14"/><color rgb="FFFFFFFF"/><name val="Arial"/></font>
    <font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Arial"/></font>
  </fonts>
  <fills count="4">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF17324D"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF0F766E"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color rgb="FFD9E2EC"/></left>
      <right style="thin"><color rgb="FFD9E2EC"/></right>
      <top style="thin"><color rgb="FFD9E2EC"/></top>
      <bottom style="thin"><color rgb="FFD9E2EC"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="3">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"><alignment vertical="top" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFill="1" applyFont="1" applyBorder="1"><alignment vertical="center"/></xf>
    <xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFill="1" applyFont="1" applyBorder="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>
"""
    )


def core_xml() -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return xml_doc(
        f"""
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
                   xmlns:dc="http://purl.org/dc/elements/1.1/"
                   xmlns:dcterms="http://purl.org/dc/terms/"
                   xmlns:dcmitype="http://purl.org/dc/dcmitype/"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Local Coding Agent Project Management</dc:title>
  <dc:creator>local-coding-agent</dc:creator>
  <cp:lastModifiedBy>local-coding-agent</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>
"""
    )


def app_xml(sheets: list[SheetData]) -> str:
    names = "".join(f"<vt:lpstr>{escape(sheet.name)}</vt:lpstr>" for sheet in sheets)
    return xml_doc(
        f"""
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
            xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>local-coding-agent</Application>
  <TitlesOfParts>
    <vt:vector size="{len(sheets)}" baseType="lpstr">{names}</vt:vector>
  </TitlesOfParts>
</Properties>
"""
    )


def xml_doc(body: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body.strip()


if __name__ == "__main__":
    raise SystemExit(main())
