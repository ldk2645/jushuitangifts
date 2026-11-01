"""将当天的实际登记结果导出为 Excel；标识符均写入文本单元格。"""

from io import BytesIO
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZipFile, ZIP_DEFLATED


NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _sheet(rows, widths):
    root = Element("worksheet", xmlns=NS)
    views = SubElement(root, "sheetViews")
    view = SubElement(views, "sheetView", workbookViewId="0")
    SubElement(view, "pane", ySplit="1", topLeftCell="A2", activePane="bottomLeft", state="frozen")
    cols = SubElement(root, "cols")
    for index, width in enumerate(widths, 1):
        SubElement(cols, "col", min=str(index), max=str(index), width=str(width), customWidth="1")
    data = SubElement(root, "sheetData")
    for index, values in enumerate(rows, 1):
        row = SubElement(data, "row", r=str(index))
        for column, value in enumerate(values):
            cell = SubElement(row, "c", r=f"{chr(65 + column)}{index}", t="inlineStr", s="1" if index == 1 else "0")
            node = SubElement(SubElement(cell, "is"), "t", {"xml:space": "preserve"})
            # XML 禁止控制字符；文本类型也避免订单号丢精度或原因被当成公式。
            node.text = "".join(ch for ch in str(value) if ch in "\t\n\r" or 32 <= ord(ch) <= 0xD7FF
                                or 0xE000 <= ord(ch) <= 0xFFFD or 0x10000 <= ord(ch) <= 0x10FFFF)[:32767]
    SubElement(root, "autoFilter", ref=f"A1:{chr(64 + len(widths))}{len(rows)}")
    return tostring(root, encoding="utf-8", xml_declaration=True)


def export_daily(day, records):
    success = [["完成时间", "订单号", "新增内部订单号", "礼品", "执行编号"]]
    failed = [["完成时间", "订单号", "失败原因", "礼品", "执行编号"]]
    for record in records:
        if record["state"] == "success":
            success.append([record["completed_at"], record["order_no"], "、".join(record["internal_order_ids"]),
                            record["gift"], record["execution_id"]])
        else:
            failed.append([record["completed_at"], record["order_no"], record["detail"], record["gift"], record["execution_id"]])
    sheets = [("成功登记", success), ("失败登记", failed)]
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>''')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        root = Element("workbook", xmlns=NS, attrib={"xmlns:r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"})
        sheet_list = SubElement(root, "sheets")
        for index, (name, rows) in enumerate(sheets, 1):
            SubElement(sheet_list, "sheet", name=name, sheetId=str(index), attrib={"r:id": f"rId{index}"})
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet(rows, [34, 28, 65, 55, 36]))
        archive.writestr("xl/workbook.xml", tostring(root, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in (1, 2))
                         + '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr("xl/styles.xml", '''<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="49" fontId="1" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>''')
    return output.getvalue()
