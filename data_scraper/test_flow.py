import asyncio
import json
import re
from playwright.async_api import async_playwright

async def get_visible_element(locator):
    count = await locator.count()
    for i in range(count):
        el = locator.nth(i)
        box = await el.bounding_box()
        vis = await el.is_visible()
        if vis and box and box['x'] >= 0 and box['y'] >= 0:
            return el
    return None

async def select_year(page, card, year):
    # Find dropdown button inside the card
    dropdown = card.locator(".select-tabs-tab-nianfen")
    drop_vis = await dropdown.is_visible()
    if not drop_vis:
        print(f"     ⚠ Dropdown not visible in card")
        return False
        
    await dropdown.click()
    await page.wait_for_timeout(1000)
    
    # Find the option
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
        print(f"     👉 Clicking dropdown option {year}")
        await found_opt.click()
        await page.wait_for_timeout(3000)
        return True
    else:
        print(f"     ⚠ Option {year} not found")
        return False

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        
        # Intercept and count records
        counts = {"luqu": {}, "zhuanye": {}}
        
        async def on_response(response):
            if "gateway.quark.cn/api/v1" in response.url:
                try:
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        body = await response.json()
                        url = response.url
                        position = ""
                        if "position=" in url:
                            m = re.search(r"position=([^&]+)", url)
                            if m:
                                position = m.group(1)
                                
                        if "luqu" in position.lower() or "fenshuxian" in position.lower():
                            data_dict = body.get("data", {})
                            
                            # check if pastyears_scorelines or major_scorelines in data_dict
                            if "pastyears_scorelines" in data_dict:
                                raw_str = data_dict["pastyears_scorelines"]
                                if raw_str:
                                    scorelines_data = json.loads(raw_str)
                                    ds = scorelines_data.get("data", {}).get("dataSource", [])
                                    if ds:
                                        y = ds[0].get("year") or "unknown"
                                        counts["luqu"][y] = len(ds)
                                        print(f"     [INTERCEPT] 录取分数线 ({y}): {len(ds)} 条记录")
                                        
                            if "major_scorelines" in data_dict:
                                raw_str = data_dict["major_scorelines"]
                                if raw_str:
                                    scorelines_data = json.loads(raw_str)
                                    ds = scorelines_data.get("data", {}).get("dataSource", [])
                                    cur_y = data_dict.get("cur_year") or "unknown"
                                    print(f"     [INTERCEPT] 专业分数线 (cur_year={cur_y}): dataSource length={len(ds)}")
                                    if ds:
                                        y = ds[0].get("year") or cur_y
                                        counts["zhuanye"][y] = len(ds)
                                        print(f"     [INTERCEPT] 专业分数线 ({y}): {len(ds)} 条记录")
                except Exception as e:
                    pass
                    
        page.on("response", on_response)
        
        # Load page with 2025 (default)
        url = 'https://vt.quark.cn/blm/gaokao-college-794/tab?app=fen_shu_xian&university_name=%E5%8C%97%E4%BA%AC%E9%82%AE%E7%94%B5%E5%A4%A7%E5%AD%A6&q=%E5%8C%97%E4%BA%AC%E9%82%AE%E7%94%B5%E5%A4%A7%E5%AD%A6&params=%7B%22province%22%3A%22%E5%B9%BF%E4%B8%9C%22%2C%22year%22%3A%222025%22%2C%22batch%22%3A%22%E6%9C%AC%E7%A7%91%E6%89%B9%22%2C%22genre%22%3A%22%E9%A6%96%E9%80%89%E7%89%A9%E7%90%86%22%7D&type=luqu'
        print(f"Loading page...")
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        
        # ── 1. 录取分数线 ──
        print("\n--- Processing 录取分数线 ---")
        card = await get_visible_element(page.locator(".fenshuxian-card"))
        if card:
            # 2025 is already loaded, now select 2024
            print("Selecting 2024 for 录取分数线...")
            await select_year(page, card, "2024")
            
            # select 2023
            print("Selecting 2023 for 录取分数线...")
            await select_year(page, card, "2023")
        else:
            print("录取分数线 card not found")
            
        # ── 2. 专业分数线 ──
        print("\n--- Processing 专业分数线 ---")
        # Click the tab for 专业分数线
        tab = await get_visible_element(page.locator(".pc-fenshu-tabs .qk-tabs-tab-content-zhuanye"))
        if tab:
            print("Clicking 专业分数线 tab...")
            await tab.click()
            await page.wait_for_timeout(2000)
            
            # The card for 专业分数线 should now be active/visible
            card = await get_visible_element(page.locator(".fenshuxian-card"))
            if card:
                # 2025 is already loaded, now select 2024
                print("Selecting 2024 for 专业分数线...")
                await select_year(page, card, "2024")
                
                # select 2023
                print("Selecting 2023 for 专业分数线...")
                await select_year(page, card, "2023")
            else:
                print("专业分数线 card not found")
        else:
            print("专业分数线 tab not found")
            
        print("\nSummary of intercepted counts:")
        print(json.dumps(counts, indent=2))
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
