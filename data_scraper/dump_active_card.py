import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        
        url = 'https://vt.quark.cn/blm/gaokao-college-794/tab?app=fen_shu_xian&university_name=%E5%8C%97%E4%BA%AC%E9%82%AE%E7%94%B5%E5%A4%A7%E5%AD%A6&q=%E5%8C%97%E4%BA%AC%E9%82%AE%E7%94%B5%E5%A4%A7%E5%AD%A6&params=%7B%22province%22%3A%22%E5%B9%BF%E4%B8%9C%22%2C%22year%22%3A%222025%22%2C%22batch%22%3A%22%E6%9C%AC%E7%A7%91%E6%89%B9%22%2C%22genre%22%3A%22%E9%A6%96%E9%80%89%E7%89%A9%E7%90%86%22%7D&type=luqu'
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        
        # Click the tab
        print("Clicking '.qk-tabs-tab-content-zhuanye'...")
        await page.click(".qk-tabs-tab-content-zhuanye")
        await page.wait_for_timeout(2000)
        
        # Find the visible fenshuxian-card
        cards = page.locator(".fenshuxian-card")
        count = await cards.count()
        print(f"Total .fenshuxian-card elements: {count}")
        
        for i in range(count):
            card = cards.nth(i)
            box = await card.bounding_box()
            visible = await card.is_visible()
            print(f"Card [{i}] Visible: {visible} | Box: {box}")
            if visible and box and box['x'] >= 0 and box['y'] >= 0:
                html = await card.inner_html()
                # print first 1500 chars of HTML
                print(f"--- CARD [{i}] HTML START ---")
                print(html[:2000])
                print(f"--- CARD [{i}] HTML END ---")
                
        await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
