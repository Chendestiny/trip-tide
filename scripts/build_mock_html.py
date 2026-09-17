"""把引擎 + 数据注入 mock 模板，产出一个**自包含的单文件 HTML**（构建期脚本）。

    cd backend && venv/Scripts/python ../scripts/build_mock_html.py
    # → dist/trip-buddy-mock.html（双击即可打开，也可直接上传发布）

它做的事只有三件：
  1. 按依赖顺序读 `frontend/src/engine/*.js`，把 `import` 语句与 `export` 关键字剥掉，
     拼成一段可内联的脚本（同一作用域，靠 ENGINE 这个 IIFE 圈起来，不污染全局）；
  2. 读 `frontend/src/engine/offline-data.json`（68 目的地 / 899 景点 / 1724 子景点）；
  3. 把两者塞进 `frontend/mock/mock.html` 的两个占位符，写出单文件。

**算法零重写**：内联进去的就是那份与 `planner.py` 逐节点一致的引擎（回归见 `local/offline-diff.py`）。
不依赖 npm / vite —— 改完引擎重跑本脚本就有新页面。

⚠️ 正式的单文件产物（复用真实 Vue 页面）走另一条：
   `cd frontend && npm run build:offline` → `venv/Scripts/python ../scripts/build_single_html.py`
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = ROOT / "frontend" / "src" / "engine"
TEMPLATE = ROOT / "frontend" / "mock" / "mock.html"
DATA = ENGINE_DIR / "offline-data.json"

# 依赖顺序（被依赖的在前）。加文件记得也加进这里，否则内联版会缺符号。
ORDER = ["pyfmt.js", "consts.js", "geo.js", "rules.js", "planner.js", "preview.js"]

PLACEHOLDER_ENGINE = "/*__ENGINE__*/"
PLACEHOLDER_DATA = "/*__DATA__*/"

RE_IMPORT = re.compile(r"^import\s+[\s\S]*?from\s+['\"][^'\"]+['\"];?[ \t]*$", re.M)
RE_EXPORT_LIST = re.compile(r"^export\s*\{[^}]*\}\s*;?[ \t]*$", re.M)
RE_EXPORT_KEYWORD = re.compile(r"^export\s+(?=(?:const|let|var|function|class)\b)", re.M)


def strip_module_syntax(name: str, src: str) -> str:
    """去掉 ESM 语法，只留声明 —— 内联后所有文件共享一个作用域。"""
    out = RE_IMPORT.sub("", src)
    out = RE_EXPORT_LIST.sub("", out)
    out = RE_EXPORT_KEYWORD.sub("", out)

    leftover = [ln for ln in out.splitlines() if re.match(r"^(import|export)\b", ln.strip())]
    if leftover:
        raise SystemExit(f"{name} 里还有没剥干净的 ESM 语句：{leftover[:3]}")
    return out


def build_engine() -> tuple[str, list[str]]:
    parts, notes = [], []
    for fname in ORDER:
        path = ENGINE_DIR / fname
        if not path.exists():
            raise SystemExit(f"缺文件：{path}")
        src = strip_module_syntax(fname, path.read_text(encoding="utf-8"))
        parts.append(f"/* ---------- {fname} ---------- */\n{src}")
        notes.append(f"{fname}（{len(src) / 1024:.0f} KB）")
    return "\n\n".join(parts), notes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="trip-buddy-mock.html")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if not TEMPLATE.exists():
        raise SystemExit(f"缺模板：{TEMPLATE}")
    if not DATA.exists():
        raise SystemExit(f"缺数据：{DATA} —— 先跑 `venv/Scripts/python ../scripts/build_offline_data.py`")

    html = TEMPLATE.read_text(encoding="utf-8")
    for ph in (PLACEHOLDER_ENGINE, PLACEHOLDER_DATA):
        if ph not in html:
            raise SystemExit(f"模板里找不到占位符 {ph}")

    engine, notes = build_engine()
    # 内联的 JSON 里若出现 `</script>` 会提前截断 HTML；转义成等价写法
    data_raw = DATA.read_text(encoding="utf-8").replace("</", "<\\/")

    html = html.replace(PLACEHOLDER_ENGINE, engine)
    html = html.replace(PLACEHOLDER_DATA, data_raw)

    out_dir = Path(args.out) if args.out else ROOT / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / args.name
    out.write_text(html, encoding="utf-8")

    parsed = json.loads(DATA.read_text(encoding="utf-8"))
    n_spot = sum(len(a.get("spots") or []) for a in parsed["attractions"])
    print("引擎：" + " + ".join(notes))
    print(f"数据：{len(parsed['cities'])} 目的地 / {len(parsed['attractions'])} 景点 / {n_spot} 子景点"
          f"（{DATA.stat().st_size / 1024:.0f} KB）")
    print(f"\n单文件产物：{out}  {out.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
