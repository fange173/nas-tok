#!/usr/bin/env python3
"""一次性手术脚本:把 index.html 内联 IIFE 切成 zero-build ES modules(已执行,留档)。

用法(仅重放):python3 scripts/split_frontend.py
校验锚点:切割区间取自手术日的 app/static/index.html;文件已变则脚本拒绝执行。
"""
import re, sys
from pathlib import Path

HTML = Path("app/static/index.html")
OUT = Path("app/static/js")

# (目标模块, [(起始行1基, 结束行exclusive), ...], 模块头注释)
CHUNKS = [
    ("util.js",   [(596, 599), (693, 695), (697, 774), (1285, 1306)], "DOM 查询 / fetch / 格式化 / 复制剪贴板"),
    ("icons.js",  [(517, 595), (604, 610), (690, 692)], "SVG 图标与媒体模式图标(纯函数,无依赖)"),
    ("state.js",  [(600, 604), (611, 684), (685, 690)], "全局可变状态与常量(预载窗口、媒体模式)"),
    ("player.js", [(784, 1156), (1156, 1264)], "音量/画面适配/沉浸模式/全局进度条 + 事件绑定"),
    ("main.js",   [(1266, 1284), (1539, 1587), (4366, 4367)], "启动路由(分享/登录/Feed)与全局装配"),
    ("share-page.js", [(1307, 1427)], "公开分享页 + 分享/取消分享数据流"),
    ("auth.js",   [(775, 784), (1428, 1499), (1499, 1538)], "登录/改密/登出表单与角色判断"),
    ("feed.js",   [(1588, 3176)], "Feed 列表/播放/滑动/轮滚/键盘/帮助"),
    ("edit-modal.js", [(3176, 3405)], "编辑模态框(视频/图片/相册)"),
    ("admin.js",  [(3405, 4101), (4305, 4366)], "后台管理(用户/库/分享/日志/标记)"),
    ("tags.js",   [(4101, 4305)], "标记模态框与标记筛选"),
]

DECL_RE = re.compile(r"^    (?:const|let|var)\s+([\w$]+)|^    (?:async\s+)?function\s+([\w$]+)")

lines = HTML.read_text(encoding="utf-8").split("\n")
full_body = "\n".join(lines[i - 1] for i in range(513, 4368))
assert full_body.strip().startswith("(function () {") and full_body.rstrip().endswith("})();"), "IIFE 边界不符,HTML 已变,勿重放"

# 1) 收集模块文本与 符号→模块 映射
module_text = {}
sym_owner = {}
for name, ranges, _note in CHUNKS:
    buf = []
    for a, b in ranges:
        buf.append("\n".join(lines[i - 1] for i in range(a, b)))
    text = "\n\n".join(buf)
    module_text[name] = text
    for line in text.split("\n"):
        m = DECL_RE.match(line)
        if m:
            sym = m.group(1) or m.group(2)
            if sym in sym_owner:
                sys.exit(f"符号重复声明: {sym} ({sym_owner[sym]} 与 {name})")
            sym_owner[sym] = name

# 2) 完整性:每一行非空白非注释行必须被某个区间覆盖(注释/空行允许遗漏)
covered = set()
for _name, ranges, _n in CHUNKS:
    for a, b in ranges:
        covered.update(range(a, b))
loose = []
for ln in range(517, 4366):
    if ln in covered:
        continue
    s = lines[ln - 1].strip()
    if s and not s.startswith("//") and not s.startswith("/*") and not s.startswith("*") and not s.endswith("*/"):
        loose.append((ln, lines[ln - 1]))
if loose:
    for ln, t in loose:
        print(f"  未覆盖行 {ln}: {t}")
    sys.exit("存在未被切割区间覆盖的有效代码行,放弃生成")

# 3) 生成模块:头注释 + import + export 化正文
def export_prefix(chunk: str) -> str:
    out = []
    for line in chunk.split("\n"):
        m = DECL_RE.match(line)
        if m and line.lstrip() == line[4:]:
            line = "    export " + line[4:]
        out.append(line)
    return "\n".join(out)

core = ["util.js", "icons.js", "state.js"]  # 无依赖基座,断言用
def ref_pattern(sym: str) -> re.Pattern:
    head = r"(?<![\w$.])" + re.escape(sym)
    tail = r"(?![\w${])" if sym.startswith("$") else r"(?![\w$])"
    return re.compile(head + tail)


for name, _ranges, note in CHUNKS:
    body = export_prefix(module_text[name])
    # 计算本模块引用的他模块符号($ 特例:$ 开头标识符须排除模板字符串 ${} 插值)
    imports = {}
    for sym, owner in sym_owner.items():
        if owner == name:
            continue
        if ref_pattern(sym).search(body):
            imports.setdefault(owner, []).append(sym)
    imp_lines = [f"import {{ {', '.join(sorted(v))} }} from './{mod}';" for mod, v in sorted(imports.items())]
    text = f"/* {name} — {note}(由 split_frontend.py 机械切割,勿手改顺序) */\n"
    if imp_lines:
        text += "\n".join(imp_lines) + "\n"
    text += "\n" + body.rstrip("\n") + "\n"
    if name in core and imp_lines:
        sys.exit(f"核心模块 {name} 不应有依赖: {imp_lines}")
    (OUT / name).write_text(text, encoding="utf-8")
    print(f"  {name}: {len(text.splitlines())} 行, imports={len(imp_lines)}")

# 4) 改写 index.html:两个 <script> 块(512-4376)替换为 module 引用
new_head = "  <script type=\"module\" src=\"/static/js/main.js?v=1\"></script>"
result = lines[:511] + [new_head] + lines[4376:]
HTML.write_text("\n".join(result), encoding="utf-8")
print(f"index.html: {len(lines)} → {len(result)} 行")
print("完成。后续:node scripts/smoke-modules.mjs 做链接/冒烟验证")
