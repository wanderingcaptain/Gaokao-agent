import asyncio
import json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        
        async def on_response(response):
            if "gateway.quark.cn/api/v1" in response.url:
                try:
                    status = response.status
                    ct = response.headers.get("content-type", "")
                    print(f"\nResponse intercepted: {response.url[:120]}")
                    print(f"  Status: {status} | Content-Type: {ct}")
                    if "json" in ct:
                        body = await response.json()
                        print(f"  Body keys: {list(body.keys()) if isinstance(body, dict) else 'Not a dict'}")
                        if isinstance(body, dict) and "data" in body:
                            data = body["data"]
                            print(f"  Data keys: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
                            if isinstance(data, dict) and "major_scorelines" in data:
                                print(f"  major_scorelines is present (length: {len(data['major_scorelines'])})")
                                # parse it
                                sub_data = json.loads(data["major_scorelines"])
                                ds = sub_data.get("data", {}).get("dataSource", [])
                                print(f"  dataSource length: {len(ds)}")
                                if ds:
                                    print(f"  First item year: {ds[0].get('year')} | major: {ds[0].get('major')}")
                except Exception as e:
                    print(f"  ❌ Error parsing response: {e}")
                
        page.on("response", on_response)
        
        url = 'https://vt.quark.cn/blm/gaokao-college-794/tab?app=fen_shu_xian&university_name=%E5%8C%97%E4%BA%AC%E9%82%AE%E7%94%B5%E5%A4%A7%E5%AD%A6&q=%E5%8C%97%E4%BA%AC%E9%82%AE%E7%94%B5%E5%A4%A7%E5%AD%A6&params=%7B%22province%22%3A%22%E5%B9%BF%E4%B8%9C%22%2C%22year%22%3A%222025%22%2C%22batch%22%3A%22%E6%9C%AC%E7%A7%91%E6%89%B9%22%2C%22genre%22%3A%22%E9%A6%96%E9%80%89%E7%89%A9%E7%90%86%22%7D&type=luqu'
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        
        # Click the tab
        print("Clicking active zhuanye tab inside .pc-fenshu-tabs...")
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
            
            # Find the visible fenshuxian-card
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
                await dropdown.click()
                await page.wait_for_timeout(1500)
                
                # Find "2024"
                opts = page.locator("div, span, li, button")
                opt_count = await opts.count()
                found_opt = None
                for i in range(opt_count):
                    opt = opts.nth(i)
                    txt = await opt.text_content()
                    if txt.strip() == "2024":
                        box = await opt.bounding_box()
                        vis = await opt.is_visible()
                        if vis and box and box['x'] >= 0 and box['y'] >= 0:
                            found_opt = opt
                            break
                
                if found_opt:
                    print("Clicking option 2024...")
                    await found_opt.click()
                    await page.wait_for_timeout(4000)
                else:
                    print("No option '2024' found!")
            else:
                print("No visible fenshuxian-card found!")
        else:
            print("No visible zhuanye tab found!")
            
        await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
