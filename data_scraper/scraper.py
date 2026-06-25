"""
夸克高考数据爬虫 v2
====================
API 已确认：https://gateway.quark.cn/api/v1
  - position=luqufenshuxian   → 录取分数线
  - position=zhuanyefenshuxian → 专业分数线

策略：用 Playwright 打开页面，直接拦截带签名的 API 请求响应，
无需破解签名算法，浏览器自动完成签名。

使用方式：
  pip install playwright
  python3 -m playwright install chromium
  python3 scraper.py

输出：
  output/{院校}_{省份}_{年份}_luqu.json     录取分数线原始数据
  output/{院校}_{省份}_{年份}_major.json    专业分数线原始数据
  output/all_records_YYYYMMDD.csv           汇总 CSV（可直接导入数据库）
"""
import asyncio
import json
import csv
import re
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

# ──────────────────────────────────────────────
# 配置：要抓取的院校 × 省份 × 年份列表
# ──────────────────────────────────────────────
TARGETS = [
    ("北京邮电大学",  "广东", "2025", "本科批", "首选物理"),
    ("北京邮电大学",  "广东", "2024", "本科批", "首选物理"),
    ("北京邮电大学",  "广东", "2023", "本科批", "首选物理"),
]

BASE_PAGE_URL = "https://vt.quark.cn/blm/gaokao-college-794/tab"
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

HEADLESS = True   # 改为 False 可以看到浏览器操作过程


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────
def build_page_url(university_name: str, province: str, year: str,
                   batch: str, genre: str) -> str:
    import urllib.parse
    params_json = json.dumps(
        {"province": province, "year": year, "batch": batch, "genre": genre},
        ensure_ascii=False
    )
    q = {
        "app": "fen_shu_xian",
        "university_name": university_name,
        "q": university_name,
        "device": "pc",
        "bar": "pure",
        "by": "tuijian",
        "by2": "general_entity_college",
        "params": params_json,
        "type": "luqu",
    }
    return BASE_PAGE_URL + "?" + urllib.parse.urlencode(q)


def parse_luqu_response(body: dict, university_name: str, province: str,
                        year: str, batch: str, subject_type: str) -> list[dict]:
    """从录取分数线 / 专业分数线 API 响应中解析结构化记录"""
    records = []
    try:
        data_dict = body.get("data", {}) if isinstance(body, dict) else {}
        
        # 1. 录取分数线 (pastyears_scorelines)
        if isinstance(data_dict, dict) and "pastyears_scorelines" in data_dict:
            raw_str = data_dict["pastyears_scorelines"]
            if raw_str:
                scorelines_data = json.loads(raw_str)
                data_source = scorelines_data.get("data", {}).get("dataSource", [])
                for item in data_source:
                    record = {
                        "university_name":  university_name,
                        "province":         province,
                        "year":             _get_int(item, ["year", "年份"]) or int(year),
                        "batch":            batch,
                        "subject_type":     subject_type,
                        "enrollment_type":  _get(item, ["type", "zslx", "招生类型"]) or "普通类",
                        "major_name":       _get(item, ["specialized", "专业组"]) or "学校录取线",
                        "min_score":        _get_int(item, ["low_score", "zdf", "最低分"]),
                        "min_rank":         _get_int(item, ["low_rank", "zdwc", "最低位次"]),
                        "enrollment_count": _get_int(item, ["luqurenshu", "rs", "人数"]),
                        "score_diff":       _get_int(item, ["low_score_diff", "pcf", "线差"]),
                    }
                    records.append(record)
                return records

        # 2. 专业分数线 (major_scorelines)
        if isinstance(data_dict, dict) and "major_scorelines" in data_dict:
            raw_str = data_dict["major_scorelines"]
            if raw_str:
                scorelines_data = json.loads(raw_str)
                data_source = scorelines_data.get("data", {}).get("dataSource", [])
                for item in data_source:
                    major_name = _get(item, ["major", "zymc", "专业名称"])
                    subtitle = _get(item, ["major_subtitle", "备注"]) or ""
                    
                    # 确定招生类型
                    enroll_type = "普通类"
                    if "中外合作" in subtitle:
                        enroll_type = "中外合作"
                    elif "强基" in subtitle:
                        enroll_type = "强基计划"
                    elif "国家专项" in subtitle:
                        enroll_type = "国家专项"
                    elif "地方专项" in subtitle:
                        enroll_type = "地方专项"
                    elif "高校专项" in subtitle:
                        enroll_type = "高校专项"

                    record = {
                        "university_name":  university_name,
                        "province":         province,
                        "year":             _get_int(item, ["year", "年份"]) or int(year),
                        "batch":            batch,
                        "subject_type":     subject_type,
                        "enrollment_type":  enroll_type,
                        "major_name":       major_name,
                        "min_score":        _get_int(item, ["low_score", "zdf", "最低分"]),
                        "min_rank":         _get_int(item, ["low_rank", "zdwc", "最低位次"]),
                        "enrollment_count": _get_int(item, ["luqurenshu", "rs", "人数"]),
                        "score_diff":       _get_int(item, ["low_score_diff", "pcf", "线差"]),
                    }
                    records.append(record)
                return records

        # 3. 备选方案：常规嵌套列表结构
        data = body
        for key in ["data", "result", "list", "items", "records"]:
            if isinstance(data, dict) and key in data:
                data = data[key]
            elif isinstance(data, list):
                break

        if not isinstance(data, list):
            # 深度搜索列表
            def find_list(obj, depth=0):
                if depth > 5:
                    return None
                if isinstance(obj, list) and len(obj) > 0:
                    return obj
                if isinstance(obj, dict):
                    for v in obj.values():
                        result = find_list(v, depth + 1)
                        if result:
                            return result
                return None
            data = find_list(body) or []

        for item in data:
            if not isinstance(item, dict):
                continue
            record = {
                "university_name":  university_name,
                "province":         province,
                "year":             year,
                "batch":            batch,
                "subject_type":     subject_type,
                "enrollment_type":  _get(item, ["enrollment_type", "zslx", "type", "招生类型", "lx"]) or "普通类",
                "major_name":       _get(item, ["major_name", "zymc", "major", "专业名称", "name", "zy"]),
                "min_score":        _get_int(item, ["min_score", "zdf", "lowest_score", "最低分", "score"]),
                "min_rank":         _get_int(item, ["min_rank", "zdwc", "lowest_rank", "最低位次", "rank", "wc"]),
                "enrollment_count": _get_int(item, ["enrollment_count", "rs", "count", "人数", "num"]),
                "score_diff":       _get_int(item, ["score_diff", "pcf", "line_diff", "批次线差", "diff"]),
            }
            records.append(record)
    except Exception as e:
        print(f"     ⚠ 解析失败: {e}")
    return records


def _get(d: dict, keys: list):
    for k in keys:
        if k in d and d[k] not in (None, "", "—", "-"):
            return d[k]
    return None


def _get_int(d: dict, keys: list):
    v = _get(d, keys)
    if v is None:
        return None
    try:
        return int(str(v).replace(",", "").replace("，", ""))
    except (ValueError, TypeError):
        return None


# ──────────────────────────────────────────────
# 核心爬取函数
# ──────────────────────────────────────────────
async def scrape_one(page, university_name: str, province: str,
                     year: str, batch: str, genre: str) -> dict:
    tag = f"{university_name}_{province}_{year}"
    result = {"luqu": [], "major": [], "raw": []}

    # ── 拦截 gateway API 响应 ──
    async def on_response(response):
        url = response.url
        if "gateway.quark.cn/api/v1" not in url:
            return
        print(f"     [DEBUG] 拦截到 API: {url[:120]}")
        try:
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = await response.json()
            position = ""
            if "position=" in url:
                m = re.search(r"position=([^&]+)", url)
                if m:
                    position = m.group(1)

            entry = {"url": url, "position": position, "body": body}
            result["raw"].append(entry)

            if "luqu" in position.lower() or "fenshuxian" in position.lower():
                records = parse_luqu_response(body, university_name, province,
                                              year, batch, genre)
                if "zhuanye" in position.lower() or "major" in position.lower():
                    result["major"].extend(records)
                    print(f"     📊 专业分数线：拦截到 {len(records)} 条")
                else:
                    result["luqu"].extend(records)
                    print(f"     📊 录取分数线：拦截到 {len(records)} 条")
        except Exception as e:
            print(f"     ❌ 拦截响应出错: {e}")

    page.on("response", on_response)

    # ── 打开页面 ──
    url = build_page_url(university_name, province, year, batch, genre)
    print(f"\n  🌐 {tag}: {url[:90]}...")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)  # 等待懒加载

        # 触发「专业分数线」Tab 点击（切换触发另一个 API）
        try:
            # 找到可见的专业分数线 Tab
            tabs = page.locator(".pc-fenshu-tabs .qk-tabs-tab-content-zhuanye")
            tab_count = await tabs.count()
            target_tab = None
            for i in range(tab_count):
                tab = tabs.nth(i)
                box = await tab.bounding_box()
                vis = await tab.is_visible()
                if vis and box and box['x'] >= 0 and box['y'] >= 0:
                    target_tab = tab
                    break
            
            if target_tab:
                await target_tab.click()
                await page.wait_for_timeout(2000)
                
                # 若目标年份不是 2025，选择对应的年份
                if year != "2025":
                    # 找到可见的 fenshuxian-card
                    cards = page.locator(".fenshuxian-card")
                    card_count = await cards.count()
                    target_card = None
                    for i in range(card_count):
                        card = cards.nth(i)
                        box = await card.bounding_box()
                        vis = await card.is_visible()
                        if vis and box and box['x'] >= 0 and box['y'] >= 0:
                            target_card = card
                            break
                    
                    if target_card:
                        dropdown = target_card.locator(".select-tabs-tab-nianfen")
                        drop_box = await dropdown.bounding_box()
                        drop_vis = await dropdown.is_visible()
                        if drop_vis and drop_box and drop_box['x'] >= 0:
                            await dropdown.click()
                            print(f"     ✅ 点击年份下拉框")
                            await page.wait_for_timeout(1500)
                            
                            # 选择年份选项 (必须在可见的视口内且文本完全匹配)
                            opts = page.locator("div, span, li, button")
                            opt_count = await opts.count()
                            found_opt = None
                            for i in range(opt_count):
                                opt = opts.nth(i)
                                txt = await opt.text_content()
                                if txt.strip() == year:
                                    box = await opt.bounding_box()
                                    vis = await opt.is_visible()
                                    if vis and box and box['x'] >= 0 and box['y'] >= 0:
                                        found_opt = opt
                                        break
                            
                            if found_opt:
                                await found_opt.click()
                                print(f"     ✅ 成功选择年份 {year}")
                                await page.wait_for_timeout(3500)
                            else:
                                print(f"     ⚠ 未找到可见的年份选项 {year}")
                        else:
                            print("     ⚠ 年份下拉框不可见或超出视口")
                    else:
                        print("     ⚠ 未找到可见的分数线卡片")
            else:
                print("     ⚠ 未找到可见的专业分数线标签")
        except Exception as e:
            print(f"     ⚠ 选择专业分数线年份 {year} 失败: {e}")

    except Exception as e:
        print(f"     ❌ 页面加载失败: {e}")

    page.remove_listener("response", on_response)

    # ── 保存原始 JSON ──
    for entry in result["raw"]:
        pos = entry["position"] or "unknown"
        raw_path = OUTPUT_DIR / f"{tag}_{pos}.json"
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)

    # ── 若 API 拦截无数据，fallback 到 DOM 解析 ──
    if not result["luqu"] and not result["major"]:
        print(f"     ⚠ API 未拦截到数据，尝试 DOM 解析...")
        result.update(await _dom_fallback(page, university_name, province, year, batch, genre))

    return result


async def _dom_fallback(page, university_name, province, year, batch, genre):
    """备用方案：直接从页面 DOM 文本中提取数据"""
    luqu, major = [], []
    try:
        dom_text = await page.evaluate("document.body.innerText")
        # 保存原始文本供调试
        txt_path = OUTPUT_DIR / f"{university_name}_{province}_{year}_dom.txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(dom_text)
        print(f"     📝 DOM 文本已保存: {txt_path.name}（请手动检查数据格式）")
    except Exception as e:
        print(f"     ❌ DOM 提取失败: {e}")
    return {"luqu": luqu, "major": major}


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
async def main():
    all_records = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome",           # 使用系统已安装的 Google Chrome，无需下载
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        page = await context.new_page()

        for (uni_name, province, year, batch, genre) in TARGETS:
            result = await scrape_one(page, uni_name, province, year, batch, genre)
            combined = result["luqu"] + result["major"]
            all_records.extend(combined)
            print(f"  ✅ {uni_name} {province} {year}：共 {len(combined)} 条记录")
            await asyncio.sleep(2)

        await browser.close()

    # ── 保存汇总 CSV ──
    if all_records:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = OUTPUT_DIR / f"all_records_{ts}.csv"
        fields = [
            "university_name", "province", "year", "batch", "subject_type",
            "enrollment_type", "major_name", "min_score", "min_rank",
            "enrollment_count", "score_diff"
        ]
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_records)
        print(f"\n🎉 完成！共 {len(all_records)} 条记录 → {csv_path}")
    else:
        print(f"\n⚠️  未获取到结构化数据，请查看 output/ 目录中的 raw JSON 文件")
        print("   可能原因：API 签名拦截失败，或页面结构已变化")
        print("   建议：查看 *_luqufenshuxian.json 文件，分析实际数据结构")

    print(f"\n📂 所有文件：{OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
