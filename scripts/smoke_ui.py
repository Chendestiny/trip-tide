"""前端冒烟测试：用无头浏览器在**两种视口**下走一遍 4 个页面 + 完整规划流程。

存在的意义：白屏这类问题**编译不报错、接口也正常**，只有真跑一遍浏览器才发现。
本项目踩过一次——`<router-view>` 外面套 `<transition mode="out-in">` 时，
离场过渡不完成会导致新页面永不挂载：路由变了、router-view 渲染成空注释，
表现就是白屏，而直接打开/硬刷新同一个 URL 却完全正常（因为不经过过渡）。

两种视口都要测，因为布局是靠媒体查询切的（宽屏网页版 / 手机 App 形态）：
    · 宽屏 1280×900 —— 多列栅格 + 右侧粘性面板 + 顶部导航
    · 手机 390×844  —— 单列 + 底部浮条 + 底部标签栏

依赖：
    pip install playwright
    浏览器用系统自带的 Edge / Chrome（channel），不需要 playwright install 下载

用法（需先起前后端）：
    cd backend && venv/Scripts/python run.py          # 终端 A
    cd frontend && npm run dev                        # 终端 B
    python scripts/smoke_ui.py                        # 终端 C
    python scripts/smoke_ui.py --skip-plan            # 跳过耗时 20s 的规划环节
"""

from __future__ import annotations

import argparse
import sys

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("缺少 playwright：pip install playwright")

BASE = "http://localhost:5176"
CITY = "%E6%88%90%E9%83%BD"          # 成都
MIN_CONTENT = 500                     # #app 内容短于这个长度即认为白屏

VIEWPORTS = [
    ("宽屏", {"width": 1280, "height": 900}),
    ("手机", {"width": 390, "height": 844}),
]


class Probe:
    def __init__(self, page, label: str) -> None:
        self.page = page
        self.label = label
        self.problems: list[str] = []
        self.errors: list[str] = []

    def watch(self) -> None:
        self.page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        self.page.on(
            "console",
            lambda m: self.errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" else None,
        )

    def check(self, name: str, *, min_len: int = MIN_CONTENT) -> int:
        html = self.page.query_selector("#app")
        n = len(html.inner_html()) if html else 0
        ok = n >= min_len
        print(f"    {'✅' if ok else '❌'} {name}  #app={n}")
        if not ok:
            self.problems.append(f"[{self.label}] {name} 疑似白屏（#app 仅 {n} 字符）")
        return n

    def drain_errors(self, name: str) -> None:
        for e in dict.fromkeys(self.errors):
            print(f"      🔴 {e[:180]}")
            self.problems.append(f"[{self.label}] {name} 控制台报错：{e[:120]}")
        self.errors.clear()


def run_viewport(browser, label: str, viewport: dict, skip_plan: bool) -> list[str]:
    print(f"\n{'=' * 72}\n视口：{label}  {viewport['width']}×{viewport['height']}\n{'=' * 72}")
    page = browser.new_page(viewport=viewport)
    pr = Probe(page, label)
    pr.watch()

    # ① 首页
    print("  ① 首页")
    page.goto(f"{BASE}/trip", wait_until="networkidle")
    page.wait_for_timeout(700)
    pr.check("首页直开")
    body = page.inner_text("body")
    if "热门城市" not in body:
        pr.problems.append(f"[{label}] 首页没渲染出「热门城市」")
    if "成都" not in body:
        pr.problems.append(f"[{label}] 首页没有城市数据（先跑 seed）")
    pr.drain_errors("首页")

    # 宽屏应该有顶部导航、手机应该有底部标签栏
    has_nav = page.is_visible(".site-nav a")
    has_tab = page.is_visible(".tabbar a")
    expect_nav, expect_tab = label == "宽屏", label == "手机"
    print(f"    顶部导航={has_nav}（期望 {expect_nav}）  底部标签栏={has_tab}（期望 {expect_tab}）")
    if has_nav != expect_nav:
        pr.problems.append(f"[{label}] 顶部导航显示状态不对（{has_nav}）")
    if has_tab != expect_tab:
        pr.problems.append(f"[{label}] 底部标签栏显示状态不对（{has_tab}）")

    # ② 点击城市 → 景点池（这一步最容易白屏）
    print("  ② 点击城市 → 景点池")
    page.get_by_text("成都", exact=True).first.click()
    page.wait_for_timeout(1200)
    pr.check("点击跳转后")
    if "/trip/pick" not in page.url:
        pr.problems.append(f"[{label}] 点击后没跳到 pick，当前 {page.url}")
    if "勾选想去的景点" not in page.inner_text("body"):
        pr.problems.append(f"[{label}] 景点池页没渲染出标题")
    pr.drain_errors("景点池")

    # ③ 勾选 + 设置 + 规划
    cards = page.query_selector_all(".att")
    print(f"    景点卡片 {len(cards)} 张")
    if len(cards) < 3:
        pr.problems.append(f"[{label}] 景点卡片只有 {len(cards)} 张")
    for c in cards[:4]:
        c.click()
        page.wait_for_timeout(60)

    # 设置面板：宽屏常驻、手机需点开
    if label == "手机":
        page.click(".panel-toggle")
        page.wait_for_timeout(350)
    page.click(".seg.days button:nth-child(2)")      # 2 天
    page.wait_for_timeout(150)
    print("    已勾选 4 个 · 选 2 天")

    # 紧凑度提示（纯硬编码接口，防抖 350ms）
    page.wait_for_timeout(1200)
    load_box = page.query_selector(".load-box")
    if load_box:
        txt = load_box.inner_text().replace("\n", " · ")
        print(f"    紧凑度提示：{txt[:80]}")
    else:
        pr.problems.append(f"[{label}] 没出现紧凑度提示")

    if skip_plan:
        print("    跳过规划（--skip-plan）")
        pr.drain_errors("景点池")
        page.close()
        return pr.problems

    page.click(".plan-btn")
    print("    等待 AI 规划（最多 90s）…")
    try:
        page.wait_for_url("**/trip/plan**", timeout=90000)
    except Exception as exc:  # noqa: BLE001
        pr.problems.append(f"[{label}] 规划后没跳到结果页：{str(exc)[:90]}")
    page.wait_for_timeout(2500)

    # ④ 结果页
    print("  ④ 结果页")
    pr.check("结果页", min_len=1200)
    body = page.inner_text("body")
    for kw in ("Day 1", "入住"):
        if kw not in body:
            pr.problems.append(f"[{label}] 结果页缺少「{kw}」")
    nodes = page.query_selector_all(".tl-node")
    print(f"    时间轴节点 {len(nodes)} 个")
    if not nodes:
        pr.problems.append(f"[{label}] 结果页没有时间轴节点")
    pr.drain_errors("结果页")

    # ⑤ 地图弹层（连开两次 —— 回归「二次打开白屏」）
    print("  ⑤ 地图弹层")
    def open_map() -> bool:
        btn = ".shell-head-right .btn" if label == "宽屏" else ".map-btn"
        el = page.query_selector(btn)
        if el and "地图" in (el.inner_text() or ""):
            el.click()
        elif page.query_selector(".map-btn"):
            page.click(".map-btn")
        page.wait_for_timeout(1600)
        return bool(page.query_selector(".map-sheet"))

    if not open_map():
        pr.problems.append(f"[{label}] 地图弹层没打开")
    else:
        print("    ✅ 第一次打开")
        page.click(".map-x")
        page.wait_for_timeout(600)
        if open_map():
            print("    ✅ 第二次打开（回归通过）")
            page.click(".map-x")
            page.wait_for_timeout(500)
        else:
            pr.problems.append(f"[{label}] 地图弹层二次打开失败（曾经的 BUG）")
    pr.drain_errors("地图")

    # ⑥ 重新生成弹窗 + 按倾向微调（纯硬编码，秒级）
    print("  ⑥ 重新生成弹窗 → 倾向微调")
    pr.errors.clear()
    regen = page.query_selector(".shell-head-right .btn:last-child")
    if regen:
        regen.click()
        page.wait_for_timeout(600)
        if page.query_selector(".dlg"):
            print("    ✅ 弹窗已打开")
            page.click(".dlg .seg.days button:nth-child(3)")   # 改成 3 天
            page.wait_for_timeout(200)
            page.click(".dlg-actions .btn.ghost")              # 按倾向微调
            page.wait_for_timeout(4000)
            txt = page.inner_text("body")
            if "3 天" in txt or "Day 3" in txt:
                print("    ✅ 微调完成，天数已生效")
            else:
                pr.problems.append(f"[{label}] 微调后没看到 3 天的结果")
        else:
            pr.problems.append(f"[{label}] 重新生成弹窗没打开")
    else:
        pr.problems.append(f"[{label}] 找不到重新生成按钮")
    pr.drain_errors("重新生成")

    # ⑦ 返回景点池要带上 city（回归「返回丢参数」）
    print("  ⑦ 返回景点池")
    pr.errors.clear()
    page.click(".back-link")
    page.wait_for_timeout(1400)
    url = page.url
    print(f"    → {url}")
    if "/trip/pick" in url and "city=" in url:
        print("    ✅ 带上了 city 参数")
    else:
        pr.problems.append(f"[{label}] 返回后 URL 缺 city：{url}")
    if "勾选想去的景点" not in page.inner_text("body"):
        pr.problems.append(f"[{label}] 返回后景点池没渲染（缺 city 参数会报错）")
    pr.drain_errors("返回")

    # ⑥ 我的
    print("  ⑧ 我的行程")
    pr.errors.clear()
    page.goto(f"{BASE}/trip", wait_until="networkidle")
    page.wait_for_timeout(400)
    if label == "宽屏":
        page.click(".site-nav a[href='/trip/me']")
    else:
        page.click(".tabbar a:nth-child(2)")
    page.wait_for_timeout(1000)
    pr.check("我的页")
    if "份方案" not in page.inner_text("body"):
        pr.problems.append(f"[{label}] 我的页没有统计区（历史没写进 localStorage？）")
    pr.drain_errors("我的页")

    # ⑦ 反复跳转（回归白屏）
    print("  ⑨ 反复跳转")
    page.goto(f"{BASE}/trip", wait_until="networkidle")
    page.wait_for_timeout(400)
    page.get_by_text("成都", exact=True).first.click()
    page.wait_for_timeout(1100)
    pr.check("二次点击城市")
    pr.drain_errors("二次跳转")
    page.close()
    return pr.problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-plan", action="store_true", help="跳过 20s 的规划环节")
    args = ap.parse_args()

    all_problems: list[str] = []
    with sync_playwright() as p:
        browser = None
        for ch in ("msedge", "chrome", None):
            try:
                browser = (
                    p.chromium.launch(channel=ch, headless=True) if ch
                    else p.chromium.launch(headless=True)
                )
                print(f"浏览器：{ch or 'playwright 自带 chromium'}")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  （跳过 {ch or 'chromium'}：{str(exc).splitlines()[0][:80]}）")
        if browser is None:
            return 1

        for label, vp in VIEWPORTS:
            all_problems += run_viewport(browser, label, vp, args.skip_plan)
        browser.close()

    print("\n" + "#" * 72)
    if all_problems:
        print(f"❌ {len(all_problems)} 个问题：")
        for x in all_problems:
            print("  -", x)
        return 1
    print("✅ 前端冒烟测试全部通过（宽屏 + 手机）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
