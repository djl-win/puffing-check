import re
import asyncio
import datetime as dt
from typing import List, Tuple, Optional, Dict, Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ================= 配置 =================
CATEGORY_URL = (
    "https://bookings.puffingbillyrailway.org.au/"
    "BookingCat/Availability/?ParentCategory=WEBEXCURSION"
)
PRODUCT_NAME = "Belgrave to Lakeside Return"
HEADLESS = True  # 本地调试想看浏览器可以改成 False
# =======================================


# ============ 状态匹配规则 ============
PAT_LIMITED = re.compile(r"limited\s+seats\s+(\d+)\s+available", re.I)
PAT_BOOKNOW = re.compile(r"\bbook\s*now\b", re.I)
PAT_FULL = re.compile(r"\bfully\s+booked\b", re.I)
PAT_NA = re.compile(r"\bnot\s+available\b", re.I)
PAT_AVAIL = re.compile(r"\bavailable\b", re.I)


def classify_status(text: str) -> Tuple[str, bool, Optional[int]]:
    """
    把单元格里的文本，归类为几种状态：
    返回: (code, is_available, seats_left)
    """
    t = (text or "").strip()
    if not t:
        return ("NA", False, None)

    m = PAT_LIMITED.search(t)
    if m:
        return ("LIMITED", True, int(m.group(1)))

    if PAT_BOOKNOW.search(t):
        return ("BOOK_NOW", True, None)

    if PAT_FULL.search(t):
        return ("FULL", False, 0)

    if PAT_NA.search(t):
        return ("NA", False, None)

    if PAT_AVAIL.search(t):
        return ("AVAILABLE", True, None)

    return ("UNKNOWN", False, None)


# ============ 工具函数 ============
def _month_year(date_str: str):
    """"14/12/2025" -> ("December 2025", 14)"""
    d = dt.datetime.strptime(date_str, "%d/%m/%Y")
    return d.strftime("%B %Y"), d.day


# ============ 打开产品页面 ============
async def open_product(page) -> bool:
    await page.goto(CATEGORY_URL, wait_until="domcontentloaded")

    # 尝试关掉 cookie 弹窗
    for label in ["Accept", "Agree", "OK", "I understand", "我知道了"]:
        try:
            await page.get_by_text(label, exact=False).click(timeout=1500)
            break
        except:
            pass

    try:
        # 🔥 最重要：真正的 Puffing Billy 产品标题选择器
        card = page.locator(
            f"h2:has-text('{PRODUCT_NAME}'), "
            f"article:has-text('{PRODUCT_NAME}'), "
            f"div.card:has-text('{PRODUCT_NAME}')"
        ).first

        await card.wait_for(state="visible", timeout=25000)
    except PWTimeout:
        print(f"[错误] 25 秒内没有找到产品标题：{PRODUCT_NAME}")
        return False
    except Exception as e:
        print(f"[错误] 选择产品标题异常: {e}")
        return False

    # 找按钮（buy / book）更稳一点
    try:
        buy = page.locator("a:has-text('Buy'), a:has-text('Book'), a").first
        await buy.click(timeout=15000)
    except Exception as e:
        print(f"[错误] 点击按钮失败: {e}")
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except:
        await page.wait_for_timeout(1000)

    return True


# ============ 用日历点选日期 ============
async def pick_date_via_calendar(page, date_str: str) -> bool:
    """
    返回:
        True  - 成功选择了日期，并且控件当前值对应的日期 == date_str
        False - 灰色不可选 / 日期超出官网范围 / 任何异常，都视为“没票卖”
    """
    try:
        # 1. 打开日期输入框
        ipt = page.locator("input#datetimepicker-input")
        await ipt.wait_for(state="visible", timeout=15000)
        await ipt.scroll_into_view_if_needed()
        await ipt.click()

        target_title, day = _month_year(date_str)

        # 2. 找到日历弹窗
        dp = page.locator(
            ".bootstrap-datetimepicker-widget:visible, "
            ".datepicker:visible, "
            ".ui-datepicker:visible"
        ).first
        await dp.wait_for(state="visible", timeout=10000)

        switch = dp.locator(
            ".datepicker-days th.datepicker-switch, "
            ".picker-switch, "
            ".ui-datepicker-title"
        ).first
        prev_btn = dp.locator(
            ".datepicker-days th.prev, th.prev, .prev, .ui-datepicker-prev"
        ).first
        next_btn = dp.locator(
            ".datepicker-days th.next, th.next, .next, .ui-datepicker-next"
        ).first

        # 3. 翻月份到目标月份
        if await switch.count() > 0:
            for _ in range(36):  # 最多翻 3 年
                title = (await switch.inner_text()).strip()
                if title.lower() == target_title.lower():
                    break
                try:
                    cur = dt.datetime.strptime(title, "%B %Y")
                    tgt = dt.datetime.strptime(target_title, "%B %Y")
                    if tgt > cur:
                        await next_btn.click()
                    else:
                        await prev_btn.click()
                except Exception:
                    await next_btn.click()
                await page.wait_for_timeout(200)

        # 4. 在当前月里精确匹配“26”、“9”这种日期
        candidates = dp.locator(".day:not(.old):not(.new)")
        cnt = await candidates.count()
        matched = None

        for i in range(cnt):
            txt = (await candidates.nth(i).inner_text()).strip()
            if txt == str(day):          # 必须完全相等，避免 1/11/21/31 混淆
                matched = candidates.nth(i)
                break

        if matched is None:
            print(f"[结果] 日历中找不到日期 {date_str}，视为没票卖。")
            return False

        # 5. 如果这个格子是 disabled（灰色），也视为没票卖
        classes = (await matched.get_attribute("class") or "").lower()
        if "disabled" in classes:
            print(f"[结果] 目标日期 {date_str} 在日历中是灰色不可选，视为没票卖。")
            return False

        # 6. 点击该日期
        await matched.click()

        # 7. 等待页面加载
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            await page.wait_for_timeout(1500)

        # 8. 再确认一次：控件当前值对应的日期是不是我们要查的
        try:
            cur_val = (await page.locator("input#datetimepicker-input").input_value()).strip()
        except Exception:
            cur_val = ""

        try:
            tgt_date = dt.datetime.strptime(date_str, "%d/%m/%Y").date()
            cur_date = dt.datetime.strptime(cur_val, "%d/%m/%Y").date()
        except Exception:
            print(f"[结果] 控件当前值无法解析（'{cur_val}'），视为没票卖。")
            return False

        if cur_date != tgt_date:
            print(f"[结果] 官网日期范围不包含 {date_str}（控件实际选中 {cur_val}），视为没票卖。")
            return False

        return True

    except Exception as e:
        print(f"[结果] 选择日期 {date_str} 时出现异常：{e}")
        print("[结果] 官网可能没有这个日期的信息，视为没票卖。")
        return False


# ============ 等待表格刷新 ============
async def wait_for_table_refresh(page):
    """
    监控 #AvailabilityTable 的 innerHTML 变化，来判断新日期的表是否已经渲染好。
    """
    table_root = page.locator("#AvailabilityTable").first
    await table_root.wait_for(state="visible", timeout=15000)

    try:
        before_len = await table_root.evaluate("el => el.innerHTML.length")
    except Exception:
        before_len = None

    print("[提示] 等待页面加载表格中...")

    if before_len is None:
        await page.wait_for_timeout(4000)
        return

    try:
        await page.wait_for_function(
            """(prev) => {
                const el = document.querySelector('#AvailabilityTable');
                if (!el) return false;
                return Math.abs(el.innerHTML.length - prev) > 500;
            }""",
            arg=before_len,
            timeout=15000,
        )
    except PWTimeout:
        print("[警告] 表格变化不明显，尝试直接读取...")
    await page.wait_for_timeout(800)


# ============ 解析表格 ============
async def read_name_and_status(table_root):
    """
    解析 AvailabilityTable，返回：
      List[(name, text, code, ok, seats)]
    """

    # 先锁定 table 容器
    table = table_root.locator(".cl_availability-table").first
    if await table.count() == 0:
        print("[警告] 没有找到 .cl_availability-table 容器")
        return []

    # 一行一个 wrap
    wraps = table.locator(".cl_availability-table__wrap")
    wcnt = await wraps.count()
    if wcnt == 0:
        print("[警告] 没有找到任何 .cl_availability-table__wrap 行")
        return []

    result = []

    for i in range(wcnt):
        wrap = wraps.nth(i)

        # 班次名称
        title = wrap.locator(".cl_availability-product__title span").first
        if await title.count() == 0:
            # 有可能是空行 / 分割行，跳过
            continue
        name = (await title.inner_text()).strip()

        # 所有列
        selects = wrap.locator(".cl_availability-product__select")
        scnt = await selects.count()
        if scnt == 0:
            continue

        # 当前日期 = 第一个日期列
        cell = selects.nth(0)

        # 取状态文本
        text = ""
        fare = cell.locator(".GBEAvailCalFirstFare").first
        if await fare.count() > 0:
            text = (await fare.inner_text()).strip()
        else:
            try:
                text = (await cell.inner_text()).strip()
            except Exception:
                text = ""

        if not text:
            aria = await cell.get_attribute("aria-label")
            if aria:
                text = aria.strip()

        code, ok, seats = classify_status(text)
        result.append((name, text or "Not Available", code, ok, seats))

    return result


# ============ 核心查询函数（给 API 用） ============
async def query_date(date_str: str) -> Dict[str, Any]:
    """
    给指定日期跑一遍官网，返回结构化结果
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page()

        try:
            await open_product(page)
            picked = await pick_date_via_calendar(page, date_str)
            if not picked:
                return {
                    "date": date_str,
                    "rows": [],
                    "available_count": 0,
                    "message": "官网无此日期可选或为灰色，不可预订"
                }

            await wait_for_table_refresh(page)
            table_root = page.locator("#AvailabilityTable").first
            await table_root.wait_for(state="visible", timeout=15000)

            rows_raw = await read_name_and_status(table_root)

            rows: List[Dict[str, Any]] = []
            available_count = 0
            for name, text, code, ok, seats in rows_raw:
                if ok:
                    available_count += 1
                rows.append({
                    "name": name,
                    "status": text,
                    "code": code,
                    "available": ok,
                    "seats": seats
                })

            return {
                "date": date_str,
                "rows": rows,
                "available_count": available_count,
                "message": "OK" if rows else "该日期无班次列表"
            }

        finally:
            await browser.close()


# ============ HTML 渲染 ============

def build_html(result: Dict[str, Any]) -> str:
    date_str = result["date"]
    rows = result["rows"]
    available_count = result["available_count"]
    message = result["message"]

    # 统计
    total = len(rows)

    # 简单 CSS + emoji 表格
    html_parts = [
        "<!doctype html>",
        "<html lang='zh-CN'>",
        "<head>",
        "<meta charset='utf-8' />",
        f"<title>🚂 Puffing Billy 余票查询 - {date_str}</title>",
        "<style>",
        "body { font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; padding: 16px; background: #f5f5f5; }",
        "h1 { font-size: 20px; margin-bottom: 8px; }",
        ".summary { margin-bottom: 12px; }",
        "table { border-collapse: collapse; width: 100%; background: #fff; }",
        "th, td { border: 1px solid #ddd; padding: 8px; font-size: 14px; }",
        "th { background: #fafafa; text-align: left; }",
        "tr:nth-child(even) { background: #f9f9f9; }",
        ".ok { color: #0a960a; font-weight: bold; }",
        ".no { color: #c00; font-weight: bold; }",
        ".code { color: #999; font-size: 12px; }",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>🚂 Puffing Billy 余票查询</h1>",
        f"<div class='summary'>📅 日期：<b>{date_str}</b><br>",
        f"🧾 班次总数：<b>{total}</b>，✅ 可订：<b>{available_count}</b><br>",
        f"ℹ️ 状态：{message}</div>",
    ]

    if not rows:
        html_parts.append("<p>😢 该日期没有可显示的班次。</p>")
    else:
        html_parts.append("<table>")
        html_parts.append(
            "<tr>"
            "<th>时间 / 班次</th>"
            "<th>状态</th>"
            "<th>是否可订</th>"
            "<th>余位</th>"
            "</tr>"
        )

        for row in rows:
            name = row["name"]
            status = row["status"]
            available = row["available"]
            seats = row["seats"]

            if available:
                emoji = "✅"
                cls = "ok"
                avail_text = "可订"
            else:
                emoji = "❌"
                cls = "no"
                avail_text = "不可订"

            if seats is not None:
                seat_text = f"🎟️ {seats} 位"
            else:
                seat_text = "—"

            html_parts.append(
                "<tr>"
                f"<td>{name}</td>"
                f"<td>{status}</td>"
                f"<td class='{cls}'>{emoji} {avail_text}</td>"
                f"<td>{seat_text}</td>"
                "</tr>"
            )

        html_parts.append("</table>")

    html_parts.append("<p style='margin-top:12px;font-size:12px;color:#999;'>"
                      "数据来源：Puffing Billy Railway 官网实时查询，仅供参考。</p>")
    html_parts.append("</body></html>")

    return "\n".join(html_parts)


# ============ FastAPI 应用 ============

app = FastAPI(title="Puffing Billy Checker")


@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <html>
      <head><meta charset="utf-8"><title>🚂 Puffing Billy 余票查询</title></head>
      <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:16px;">
        <h1>🚂 Puffing Billy 余票查询 API</h1>
        <p>示例：</p>
        <ul>
          <li>HTML 表格：<code>/run?date=15/12/2025</code></li>
          <li>JSON 数据：<code>/api?date=15/12/2025</code></li>
        </ul>
      </body>
    </html>
    """


@app.get("/run", response_class=HTMLResponse)
async def run_html(date: str = Query(..., description="查询日期，格式 dd/MM/YYYY，例如 15/12/2025")):
    result = await query_date(date)
    html = build_html(result)
    return HTMLResponse(content=html)


@app.get("/api", response_class=JSONResponse)
async def run_json(date: str = Query(..., description="查询日期，格式 dd/MM/YYYY，例如 15/12/2025")):
    result = await query_date(date)
    return JSONResponse(content=result)


# 本地直接运行：python server.py
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)

