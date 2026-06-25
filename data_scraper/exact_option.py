import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        
        # Intercept and log gateway requests
        def on_request(request):
            if "gateway.quark.cn/api/v1" in request.url:
                print(f"Request: {request.url[:150]}")
                
        page.on("request", on_request)
        
        url = 'https://vt.quark.cn/blm/gaokao-college-794/tab?app=fen_shu_xian&university_name=%E5%8C%97%E4%BA%AC%E9%82%AE%E7%94%B5%E5%A4%A7%E5%AD%A6&q=%E5%8C%97%E4%BA%AC%E9%82%AE%E7%94%B5%E5%A4%A7%E5%AD%A6&params=%7B%22province%22%3A%22%E5%B9%BF%E4%B8%9C%22%2C%22year%22%3A%222025%22%2C%22batch%22%3A%22%E6%9C%AC%E7%A7%91%E6%89%B9%22%2C%22genre%22%3A%22%E9%A6%96%E9%80%89%E7%89%A9%E7%90%86%22%7D&type=luqu'
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        
        # Find all .qk-tabs-tab-content-zhuanye inside .pc-fenshu-tabs
        tabs = page.locator(".pc-fenshu-tabs .qk-tabs-tab-content-zhuanye")
        tab_count = await tabs.count()
        target_tab = None
        for i in range(tab_count):
            tab = tabs.nth(i)
            box = await tab.bounding_box()
            vis = await tab.is_visible()
            print(f"Tab [{i}] Visible: {vis} | Box: {box}")
            if vis and box and box['x'] >= 0 and box['y'] >= 0:
                target_tab = tab
                break
                
        if target_tab:
            print("Clicking active zhuanye tab inside .pc-fenshu-tabs...")
            await target_tab.click()
            await page.wait_for_timeout(2000)
            
            # Find the visible fenshuxian-card
            cards = page.locator(".fenshuxian-card")
            card_count = await cards.count()
            print(f"Total .fenshuxian-card elements after tab click: {card_count}")
            
            target_card_index = -1
            for i in range(card_count):
                card = cards.nth(i)
                box = await card.bounding_box()
                vis = await card.is_visible()
                print(f"Card [{i}] Visible: {vis} | Box: {box}")
                if vis and box and box['x'] >= 0 and box['y'] >= 0:
                    target_card_index = i
                    break
                    
            if target_card_index != -1:
                # Find dropdown button inside this specific visible card
                target_card = cards.nth(target_card_index)
                dropdown = target_card.locator(".select-tabs-tab-nianfen")
                drop_box = await dropdown.bounding_box()
                drop_vis = await dropdown.is_visible()
                print(f"Dropdown inside active card: Visible={drop_vis} | Box={drop_box}")
                
                if drop_vis and drop_box and drop_box['x'] >= 0:
                    print("Clicking year dropdown...")
                    await dropdown.click()
                    await page.wait_for_timeout(1500)
                    
                    # Find exact "2024" option inside dropdown
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
                                tag = await opt.evaluate("el => el.tagName")
                                cls = await opt.evaluate("el => el.className")
                                print(f"Found exact option 2024: TAG={tag}, CLASS={cls}, BOX={box}")
                                found_opt = opt
                                break
                                
                    if found_opt:
                        print("Clicking option 2024...")
                        await found_opt.click()
                        await page.wait_for_timeout(4000)
                    else:
                        print("No exact option '2024' found!")
                else:
                    print("Dropdown inside active card is not visible or offscreen!")
            else:
                print("No visible fenshuxian-card found in viewport after tab click!")
        else:
            print("No visible zhuanye tab inside .pc-fenshu-tabs found!")
            
        await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
