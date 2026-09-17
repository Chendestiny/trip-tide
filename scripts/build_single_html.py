"""把 `frontend/dist-offline/` 的产物内联成一个单文件 HTML（**构建期脚本**）。

    cd backend && venv/Scripts/python ../scripts/build_single_html.py
    可选：../scripts/build_single_html.py --name 我的作品.html

为什么要这一步：参赛/发布要的是**一个能直接打开的单文件网页**（资料库上传单文件或 ZIP），
而 Vite 的默认产物是 index.html + assets/*.js + assets/*.css 三个东西。
单文件形态下没有「旁边的资源文件」，必须内联。

⚠️ 前置条件是 `npm run build:offline`（见 frontend/vite.config.js 的 `mode === 'offline'`）：
   · 它把接口实现切到 `trip-api-local.js`（无后端）
   · 路由切到 hash 模式（`/p/{id}` 这种静态托管下 history 深链会 404）
   · 关掉 chunk 拆分（否则内联进去的 <script> 里再 import 相对路径会 404）

本脚本只做字符串替换，不做打包 —— 依赖关系已经在 Vite 那一步解决完了。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "frontend" / "dist-offline"


def inline(html: str) -> tuple[str, list[str]]:
    notes: list[str] = []

    # ---- <script type="module" ... src="./assets/xxx.js" ...></script> → 内联 ----
    def script_repl(m: re.Match) -> str:
        src = m.group("src")
        path = DIST / src.lstrip("./")
        if not path.exists():
            raise SystemExit(f"引用的脚本不存在：{path}（先跑 npm run build:offline）")
        code = path.read_text(encoding="utf-8")
        # 内联脚本里出现 `</script>` 会提前截断 HTML —— 转义成 JSON 里等价的写法
        code = code.replace("</script>", "<\\/script>")
        notes.append(f"内联 JS {src}（{path.stat().st_size / 1024:.0f} KB）")
        return f"<script type=\"module\">\n{code}\n</script>"

    html = re.sub(
        r'<script[^>]*\bsrc="(?P<src>[^"]+)"[^>]*></script>',
        script_repl,
        html,
    )

    # ---- <link rel="stylesheet" href="./assets/xxx.css"> → 内联 ----
    def link_repl(m: re.Match) -> str:
        href = m.group("href")
        path = DIST / href.lstrip("./")
        if not path.exists():
            return m.group(0)  # 不是本地样式（外链）就原样留着
        css = path.read_text(encoding="utf-8")
        notes.append(f"内联 CSS {href}（{path.stat().st_size / 1024:.0f} KB）")
        return f"<style>\n{css}\n</style>"

    html = re.sub(
        r'<link[^>]*\brel="stylesheet"[^>]*\bhref="(?P<href>[^"]+)"[^>]*>',
        link_repl,
        html,
    )

    # modulepreload 的指向已被内联，留着只会请求一个不存在的文件
    html = re.sub(r'<link[^>]*\brel="modulepreload"[^>]*>\s*', "", html)
    return html, notes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="trip-buddy.html", help="输出的文件名（默认 trip-buddy.html）")
    ap.add_argument("--out", default="", help="输出目录，默认写到仓库根的 dist/")
    args = ap.parse_args()

    index = DIST / "index.html"
    if not index.exists():
        raise SystemExit(f"没找到 {index} —— 先 `cd frontend && npm run build:offline`")

    html, notes = inline(index.read_text(encoding="utf-8"))

    out_dir = Path(args.out) if args.out else ROOT / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / args.name
    out.write_text(html, encoding="utf-8")

    left = [p.name for p in (DIST / "assets").glob("*")] if (DIST / "assets").exists() else []
    print("\n".join(notes))
    print(f"\n单文件产物：{out}  {out.stat().st_size / 1024:.0f} KB")
    if left:
        print(f"（dist-offline/assets 里还有 {len(left)} 个文件，已全部内联，上传时不用带）")


if __name__ == "__main__":
    main()
