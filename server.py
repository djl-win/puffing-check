import re
import datetime as dt
from typing import List, Dict, Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ================= 配置 =================
CATEGORY_URL = (
    "https://bookings.puffingbillyrailway.org.au/"
    "BookingCat/Availability/?ParentCategory=WEBEXCURSION"
)
PRODUCT_NAME = "Belgrave to Lakeside Return"
HEADLESS = True  # Railway / Docker 上建议 True
# =======================================

# ============ 状态匹配规则 ============
PAT_LIMITED = re.compile(r"limited\s+seats\s+(\d+)\s+available", re.I)
PAT_BOOKNOW = re.compile(r"\bbook\s*now\b", re.I)
PAT_FULL = re.compile(r"\bfully\s+booked\b", re.I)
PAT_NA = re.compile(r"\bnot\s+available\b", re.I)
PAT_AVAIL = re.compile(r"\bavailable\b", re.I)


def classify_status(text: str):
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
    """
    打开 Puffing Billy 分类页，并进入目标产品详情。
    返回:
        True  - 成功打开产品
        False - 没找到产品 / 结构变化 / 异常
    """
    await page.goto(CATEGORY_URL, wait_until="domcontentloaded")

    # 尝试关掉 cookie / 提示弹窗
    for label in ["Accept", "Agree", "OK", "I understand", "我知道了"]:
        try:
            await page.get_by_text(label, exact=False).click(timeout=1500)
            break
        except Exception:
            pass

    try:
        # 找到包含产品名的卡片
        card = page.locator(
            f"article:has-text('{PRODUCT_NAME}'), "
            f"div.card:has-text('{PRODUCT_NAME}')"
        ).first

        await card.wait_for(state="visible", timeout=25000)
    except PWTimeout:
        print(f"[错误] 在分类页中 25 秒内没有找到产品卡片：{PRODUCT_NAME}")
        return False
    except Exception as e:
        print(f"[错误] 打开产品卡片时出现异常: {e}")
        return False

    # 找“Buy Now / Book Now”按钮
    buy = card.locator(
        "a:has-text('BUY NOW'), a:has-text('Buy Now'), a:has-text('Book Now')"
    )
    if await buy.count() == 0:
        buy = card.locator("a").first

    onclick_js = await buy.first.get_attribute("onclick")
    try:
        if onclick_js and "changeCategory" in onclick_js:
            await page.evaluate(onclick_js)  # 直接执行 changeCategory(...)
        else:
            await buy.first.click(timeout=12000)
    except Exception as e:
        print(f"[错误] 点击产品按钮失败: {e}")
        return False

    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
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
            if txt == str(day):
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
async def read_name_and_status(table_root) -> List[Dict[str, Any]]:
    """
    返回每一行的字典：
    {
      "name": 班次名称,
      "status_text": 原始状态文本,
      "code": 归类状态码,
      "available": 是否可订,
      "seats_left": 剩余座位（可能为 None）
    }
    """
    table = table_root.locator(".cl_availability-table").first
    if await table.count() == 0:
        print("[警告] 没有找到 .cl_availability-table 容器")
        return []

    wraps = table.locator(".cl_availability-table__wrap")
    wcnt = await wraps.count()
    if wcnt == 0:
        print("[警告] 没有找到任何 .cl_availability-table__wrap 行")
        return []

    result: List[Dict[str, Any]] = []

    for i in range(wcnt):
        wrap = wraps.nth(i)

        # 班次名称
        title = wrap.locator(".cl_availability-product__title span").first
        if await title.count() == 0:
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
        result.append(
            {
                "name": name,
                "status_text": text or "Not Available",
                "code": code,
                "available": ok,
                "seats_left": seats,
            }
        )

    return result


# ============ 主查询逻辑 ============
async def query_date(date_str: str) -> Dict[str, Any]:
    """
    返回统一结构：
    {
        "ok": bool,
        "message": str,
        "date": "15/12/2025",
        "rows": [ {...}, ... ]
    }
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page()

        try:
            # 1. 进入产品
            ok = await open_product(page)
            if not ok:
                return {
                    "ok": False,
                    "message": f"官网页面上找不到产品『{PRODUCT_NAME}』，可能结构已改变或被重定向。",
                    "date": date_str,
                    "rows": [],
                }

            # 2. 日历中点击目标日期
            picked = await pick_date_via_calendar(page, date_str)
            if not picked:
                return {
                    "ok": False,
                    "message": f"官网没有 {date_str} 可售班次（日期不可选或超出范围）。",
                    "date": date_str,
                    "rows": [],
                }

            # 3. 等待表格刷新
            await wait_for_table_refresh(page)

            # 4. 读取表格
            table_root = page.locator("#AvailabilityTable").first
            await table_root.wait_for(state="visible", timeout=15000)

            rows = await read_name_and_status(table_root)
            if not rows:
                return {
                    "ok": False,
                    "message": f"官网没有 {date_str} 的班次列表，视为没票卖。",
                    "date": date_str,
                    "rows": [],
                }

            return {
                "ok": True,
                "message": "success",
                "date": date_str,
                "rows": rows,
            }

        finally:
            await browser.close()


# ================= FastAPI 部分 =================

app = FastAPI(title="Puffing Billy Ticket Checker")


@app.get("/", response_class=HTMLResponse)
async def index():
    html = """
    <html>
      <head>
        <meta charset="utf-8" />
        <title>Puffing Billy 余票查询 API</title>
      </head>
      <body>
        <h1>🚂 Puffing Billy 余票查询 API</h1>
        <p>示例：</p>
        <ul>
          <li>HTML 表格：<code>/run?date=15/12/2025</code></li>
          <li>JSON 数据：<code>/api?date=15/12/2025</code></li>
        </ul>
        <p>日期格式：<b>dd/MM/yyyy</b>（例如：15/12/2025）。</p>
      </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/api", response_class=JSONResponse)
async def run_api(date: str = Query(..., description="日期，格式 dd/MM/yyyy")):
    """
    返回 JSON 结构：
    {
      ok: bool,
      message: str,
      date: str,
      rows: [
        {
          name, status_text, code, available, seats_left
        }, ...
      ]
    }
    """
    result = await query_date(date)
    return JSONResponse(content=result)


@app.get("/run", response_class=HTMLResponse)
async def run_html(date: str = Query(..., description="日期，格式 dd/MM/yyyy")):
    """
    返回 HTML 表格版本。
    """
    result = await query_date(date)

    if not result["ok"]:
        # 业务失败，简单提示一下
        html = f"""
        <html>
          <head>
            <meta charset="utf-8" />
            <title>Puffing Billy 余票查询</title>
          </head>
          <body>
            <h1>🚂 Puffing Billy 余票查询</h1>
            <p><b>日期：</b>{result['date']}</p>
            <p>❌ {result['message']}</p>
          </body>
        </html>
        """
        return HTMLResponse(content=html, status_code=200)

    rows = result["rows"]

    # 统计可订数量
    available_count = sum(1 for r in rows if r["available"])

    # 生成表格
    table_rows_html = ""
    for r in rows:
        tag = "✅ 可订" if r["available"] else "❌ 不可订"
        extra = f"（余位 {r['seats_left']}）" if r["seats_left"] is not None else ""
        table_rows_html += f"""
        <tr>
          <td>{r['name']}</td>
          <td>{r['status_text']}</td>
          <td>{tag} {extra}</td>
          <td>{r['code']}</td>
        </tr>
        """

    html = f"""
    <html>
      <head>
        <meta charset="utf-8" />
        <title>Puffing Billy 余票查询</title>
        <style>
          body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            padding: 20px;
          }}
          table {{
            border-collapse: collapse;
            min-width: 600px;
          }}
          th, td {{
            border: 1px solid #ccc;
            padding: 6px 10px;
            text-align: left;
          }}
          th {{
            background: #f5f5f5;
          }}
        </style>
      </head>
      <body>
        <h1>🚂 Puffing Billy 余票查询</h1>
        <p><b>日期：</b>{result['date']}</p>
        <p>🟢 可订班次数量：<b>{available_count}</b></p>
        <table>
          <thead>
            <tr>
              <th>班次名称</th>
              <th>官网状态</th>
              <th>是否可订</th>
              <th>状态码</th>
            </tr>
          </thead>
          <tbody>
            {table_rows_html}
          </tbody>
        </table>
      </body>
    </html>
    """
    return HTMLResponse(content=html)
