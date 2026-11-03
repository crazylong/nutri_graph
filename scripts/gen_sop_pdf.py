# -*- coding: utf-8 -*-
"""生成 docs/sop/ 下的两份 SOP PDF：
1. 食材录入的整个流程.pdf
2. 知识库上传的整个流程.pdf
仅在本机（macOS）运行，产出静态文档，不依赖项目 venv。
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "sop"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------- 字体 ----------
_FONT = "/System/Library/Fonts/STHeiti Medium.ttc"
pdfmetrics.registerFont(TTFont("Hei", _FONT))

# ---------- 颜色（养生了么绿主题） ----------
GREEN = colors.HexColor("#2f7d5d")
GREEN_DARK = colors.HexColor("#256a4d")
GREEN_LIGHT = colors.HexColor("#eaf4ee")
GREEN_LINE = colors.HexColor("#cfe3d8")
INK = colors.HexColor("#1a241e")
INK2 = colors.HexColor("#55635b")
INK3 = colors.HexColor("#87948b")
ORANGE = colors.HexColor("#e6a23c")
RED = colors.HexColor("#c45656")
WHITE = colors.white

# ---------- 样式 ----------
def _styles():
    s = getSampleStyleSheet()
    base_font = "Hei"

    title = ParagraphStyle("title", fontName=base_font, fontSize=22, leading=30,
                           textColor=GREEN_DARK, spaceAfter=4, alignment=TA_CENTER)
    subtitle = ParagraphStyle("subtitle", fontName=base_font, fontSize=10.5, leading=16,
                              textColor=INK3, alignment=TA_CENTER, spaceAfter=14)
    h1 = ParagraphStyle("h1", fontName=base_font, fontSize=15, leading=22,
                        textColor=GREEN_DARK, spaceBefore=16, spaceAfter=8)
    h2 = ParagraphStyle("h2", fontName=base_font, fontSize=12, leading=18,
                        textColor=INK, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("body", fontName=base_font, fontSize=10, leading=16,
                          textColor=INK, spaceAfter=4)
    body2 = ParagraphStyle("body2", fontName=base_font, fontSize=10, leading=16,
                           textColor=INK2, spaceAfter=4)
    tip = ParagraphStyle("tip", fontName=base_font, fontSize=9.5, leading=15,
                         textColor=INK2, spaceAfter=4)
    stepnum = ParagraphStyle("stepnum", fontName=base_font, fontSize=11, leading=14,
                             textColor=WHITE, alignment=TA_CENTER)
    return dict(title=title, subtitle=subtitle, h1=h1, h2=h2, body=body, body2=body2,
                tip=tip, stepnum=stepnum)


ST = _styles()


def _step_circle(n: str) -> Table:
    """带数字的圆形步骤标记（用表格模拟）。"""
    t = Table([[n]], colWidths=[16 * mm], rowHeights=[16 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GREEN),
        ("TEXTCOLOR", (0, 0), (-1, -1), WHITE),
        ("FONTNAME", (0, 0), (-1, -1), "Hei"),
        ("FONTSIZE", (0, 0), (-1, -1), 13),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _step_block(num: str, title: str, body_lines: list[str], tip: str | None = None):
    """一个步骤卡片：左侧圆形编号 + 右侧标题/正文/提示。"""
    right_flow = []
    right_flow.append(Paragraph(title, ST["h2"]))
    for line in body_lines:
        right_flow.append(Paragraph(line, ST["body"]))
    if tip:
        right_flow.append(Paragraph(f"<font color='#2f7d5d'>▸</font> {tip}", ST["tip"]))

    left = _step_circle(num)
    right_table = Table([[right_flow]], colWidths=[None])
    right_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))

    outer = Table([[left, right_table]], colWidths=[20 * mm, 156 * mm])
    outer.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GREEN_LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.6, GREEN_LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return KeepTogether([outer, Spacer(1, 8)])


def _field_table(rows: list[tuple[str, str]]) -> Table:
    """字段说明表格。rows: (列名/项, 说明)"""
    data = [[Paragraph("<b>项目</b>", ST["h2"]),
             Paragraph("<b>说明</b>", ST["h2"])]]
    for a, b in rows:
        data.append([Paragraph(a, ST["body2"]), Paragraph(b, ST["body2"])])
    t = Table(data, colWidths=[42 * mm, 134 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), GREEN),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, -1), "Hei"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, GREEN_LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.4, GREEN_LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _note_box(text: str, color=ORANGE) -> Table:
    t = Table([[Paragraph(text, ST["tip"])]], colWidths=[176 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fdf6ec")),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#f3e1c0")),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _build_doc(path: Path, title: str, subtitle: str, flow: list):
    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=title, author="养生了么 · nutri_graph",
    )

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Hei", 8)
        canvas.setFillColor(INK3)
        canvas.drawString(20 * mm, 10 * mm, "养生了么 · nutri_graph 操作手册")
        canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, f"第 {doc_.page} 页")
        canvas.setStrokeColor(GREEN_LINE)
        canvas.setLineWidth(0.5)
        canvas.line(20 * mm, 14 * mm, A4[0] - 20 * mm, 14 * mm)
        canvas.restoreState()

    doc.build(flow, onFirstPage=_footer, onLaterPages=_footer)


# ============================================================
# 文档一：食材录入的整个流程
# ============================================================
def build_food_import() -> Path:
    flow = []
    flow.append(Paragraph("食材录入的整个流程", ST["title"]))
    flow.append(Paragraph("上传 → 校验 → 提交审核 → 合并进知识图谱 → 可被检索", ST["subtitle"]))

    flow.append(Paragraph("一、流程总览", ST["h1"]))
    flow.append(Paragraph(
        "把食物成分数据录入系统，标准路径只有四步：<b>① 下载模板并填写 → ② 上传表格 → "
        "③ 逐行校验并预览 → ④ 提交审核、通过后合并进图谱</b>。"
        "如果只有一两条数据，也可以跳过表格、直接在页面里「手工录入一条」。", ST["body"]))
    flow.append(Paragraph(
        "整个流程的校验都在服务端完成：单位写错、数值越界、分类不在枚举里，都会在预览阶段逐行标出，"
        "不会悄悄存进去。提交后数据进入<b>和抓取数据同一条审核队列</b>，通过后才进图谱，"
        "问答、图谱探索、食材对比就都能查到了。", ST["body"]))

    flow.append(Paragraph("二、操作步骤", ST["h1"]))

    flow.append(_step_block(
        "1", "下载模板并填写",
        [
            "在「批量导入」页面（管理后台左侧菜单），点击<b>下载 Excel 模板</b>或<b>下载 CSV 模板</b>。",
            "模板共 <b>23 列</b>，第一行为列名；列名写成「能量(kcal)」或「能量」都能被识别。",
            "必填项：<b>名称、分类、来源</b>；营养成分列空着表示「未测定」，不会被当成 0。",
        ],
        tip="单次最多 500 行、文件 1MB 以内；「来源」列建议填书名 + 版本 + 页码或 URL。",
    ))
    flow.append(_step_block(
        "2", "上传表格",
        [
            "把填好的 .xlsx / .csv 文件拖进上传区，或点击选择文件。",
            "也可以使用「手工录入一条」表单：填写名称、分类、性味、别名、英文名、学名，以及每 100 克可食部的营养成分。",
        ],
        tip="手工录入和表格导入进的是同一个审核队列。",
    ))
    flow.append(_step_block(
        "3", "逐行校验与预览",
        [
            "上传后系统返回校验汇总：<b>总行数 / 可提交 / 警告 / 错误</b> 四类。",
            "预览表格会逐行给出判定：<b>可导入、可导入·有警告、错误、文件内重复、已存在</b>。",
            "错误行会标出具体字段与原因（单位、数值越界、分类枚举等），需修正后重新上传。",
        ],
        tip="有「错误」的行不会被提交；「警告」行可提交但建议先确认。",
    ))
    flow.append(_step_block(
        "4", "提交审核并合并进图谱",
        [
            "确认无误后点击<b>提交审核</b>，数据进入审核队列（新增 / 覆盖分别统计）。",
            "提交后可点<b>一键整批通过并合并</b>，系统把候选节点与关系合并进 Neo4j 知识图谱。",
            "合并完成后，问答、图谱探索、食材对比即可检索到新录入的食材。",
        ],
        tip="合并结果会显示：通过条数、合并节点数、边数；跳过的条数也会列出。",
    ))

    flow.append(Paragraph("三、必填字段说明", ST["h1"]))
    flow.append(_field_table([
        ("名称", "食材名称，必填，最多 40 字"),
        ("分类", "食材分类，必填，必须在枚举范围内"),
        ("来源", "数据出处，必填；书名 + 版本 + 页码或 URL"),
        ("性味 / 别名 / 英文名 / 学名", "选填；别名多个用分号「;」分隔"),
        ("营养成分（能量/蛋白/脂肪/碳水…）", "选填；空着 = 未测定，不会当成 0"),
        ("备注", "选填；不进图谱，仅留档"),
    ]))
    flow.append(Spacer(1, 6))

    flow.append(Paragraph("四、注意事项", ST["h1"]))
    flow.append(_note_box(
        "• 校验在服务端做，宁可标错也不静默入库。<br/>"
        "• 请只导入你有权使用的数据；中国食物成分表版权归中国疾控中心营养与健康所，商业使用需另行授权。<br/>"
        "• 提交审核后需要人工（或一键）通过才会进图谱，不是上传即生效。",
    ))

    path = OUT_DIR / "食材录入的整个流程.pdf"
    _build_doc(path, "食材录入的整个流程", "上传 → 校验 → 提交审核 → 合并进知识图谱", flow)
    return path


# ============================================================
# 文档二：知识库上传的整个流程
# ============================================================
def build_kb_upload() -> Path:
    flow = []
    flow.append(Paragraph("知识库上传的整个流程", ST["title"]))
    flow.append(Paragraph("添加文档 → 提交待审 → 审核通过 → 解析入库 → 可被问答检索", ST["subtitle"]))

    flow.append(Paragraph("一、流程总览", ST["h1"]))
    flow.append(Paragraph(
        "企业知识库用于沉淀制度、技术等文档，上传后通过向量检索支撑「企业知识库」问答。"
        "标准路径：<b>① 登录管理后台 → ② 添加文档（租户 / 标题 / 分类 / 文件）→ "
        "③ 提交进入待审 → ④ 审核通过 → ⑤ 系统解析并向量化入库 → ⑥ 知识库问答可检索</b>。", ST["body"]))
    flow.append(Paragraph(
        "文档只接收 <b>md / markdown / pdf / docx</b> 四种格式；标题必填；分类分「技术类」「制度类」「问答类」三类。"
        "审核通过后系统才会解析、切片、向量化（写入 Milvus），通过后检索结果立刻更新。", ST["body"]))

    flow.append(Paragraph("二、操作步骤", ST["h1"]))

    flow.append(_step_block(
        "1", "登录管理后台",
        [
            "打开管理控制台（/console/），用手机号验证码登录；仅 <b>admin 角色</b>可进入。",
            "登录后进入左侧菜单的「<b>知识库查询</b>」页面（对应路由 /kb）。",
        ],
        tip="若未登录或角色非 admin，系统会自动跳转到登录页。",
    ))
    flow.append(_step_block(
        "2", "添加文档",
        [
            "在「知识库查询」页点击右上角<b>添加</b>按钮，弹出添加文档对话框。",
            "填写：<b>租户</b>（tenant_id）、<b>标题</b>（必填）、<b>分类</b>（技术类 / 制度类 / 问答类）。",
            "选择要上传的文件（仅 .md / .markdown / .pdf / .docx），点击保存。",
        ],
        tip="编辑已有文档时不选文件，则只改标题、租户和分类，并下线已有向量。",
    ))
    flow.append(_step_block(
        "3", "提交进入待审",
        [
            "保存后文档进入「<b>待审（pending）</b>」状态，此时尚未入库、检索不到。",
            "在「知识库查询」页可按租户、标题、分类、上传人、状态筛选查看。",
        ],
        tip="驳回的文档会显示「已驳回」，可查看驳回原因后修改再提交。",
    ))
    flow.append(_step_block(
        "4", "审核通过",
        [
            "进入左侧菜单「<b>知识库审核</b>」页（路由 /kb-review），切换到「待审」标签。",
            "对目标文档点<b>通过</b>；或点<b>驳回</b>并填写原因（可空）。",
        ],
        tip="审核页按「待审 / 已通过 / 已驳回」三个标签切换，便于集中处理。",
    ))
    flow.append(_step_block(
        "5", "解析入库并检索",
        [
            "通过后系统自动：解析文件（PDF 走版面 OCR、docx/md 直接解析）→ 切片 → 向量化 → 写入 Milvus。",
            "文档状态变为「<b>已通过（approved）</b>」，企业知识库问答即刻可检索到该文档内容。",
            "若解析失败，状态回退为「待审」并记录错误原因，修正后可重新通过。",
        ],
        tip="删除文档后，检索里立刻不再返回该篇。",
    ))

    flow.append(Paragraph("三、字段与限制说明", ST["h1"]))
    flow.append(_field_table([
        ("租户（tenant_id）", "归属租户，必填；隔离不同企业的知识库"),
        ("标题", "文档标题，必填"),
        ("分类（doc_kind）", "技术类 tech / 制度类 policy / 问答类 qa（问答类按 Q/A 成对切块）"),
        ("文件格式", "仅 md / markdown / pdf / docx"),
        ("图片大小限制", "文档内图片单张不超过 10MB（超过 5MB 会警告）"),
        ("状态", "待审 pending / 已通过 approved / 已驳回 rejected"),
    ]))
    flow.append(Spacer(1, 6))

    flow.append(Paragraph("四、列表中的「编辑」与「删除」", ST["h1"]))
    flow.append(Paragraph(
        "在「知识库查询」页，每一行数据的最右侧「操作」列都有两个按钮：<b>编辑</b>和<b>删除</b>。", ST["body"]))
    flow.append(_step_block(
        "1", "编辑",
        [
            "点击<b>编辑</b>，弹出「编辑文档」对话框，可修改<b>租户、标题、分类</b>。",
            "若重新选择了文件，会用新文件<b>替换原文件</b>，并重建向量（重新入库）。",
            "若<b>不选文件</b>，则只改标题、租户和分类，同时<b>下线已有向量</b>——即文档回到待审状态，需重新审核通过才可被检索。",
        ],
        tip="改内容建议重新传文件；只改标题/分类等元信息则不必传文件。",
    ))
    flow.append(_step_block(
        "2", "删除",
        [
            "点击<b>删除</b>（红色），系统弹确认框提示「删除后检索里立刻没有这篇」。",
            "确认后该文档及其向量立即下线，知识库问答<b>立刻不再返回</b>该篇内容。",
            "此操作不可恢复，请确认后再删。",
        ],
        tip="删除是永久性操作，与「驳回」不同（驳回可重新提交，删除则彻底移除）。",
    ))

    flow.append(Paragraph("五、注意事项", ST["h1"]))
    flow.append(_note_box(
        "• 上传只是「待审」，必须审核通过才真正入库、可检索。<br/>"
        "• 空文件、无标题、格式不支持会在保存阶段直接被拒绝。<br/>"
        "• 向量检索依赖 Milvus；若 Milvus 未部署，向量检索会降级，图问答不受影响。",
    ))

    path = OUT_DIR / "知识库上传的整个流程.pdf"
    _build_doc(path, "知识库上传的整个流程", "添加文档 → 提交待审 → 审核通过 → 解析入库", flow)
    return path


if __name__ == "__main__":
    p1 = build_food_import()
    p2 = build_kb_upload()
    print("生成完成：")
    print(" ", p1)
    print(" ", p2)
