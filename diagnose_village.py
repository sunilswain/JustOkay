"""
Diagnostic script to investigate why a specific village hangs during scraping.
Runs interactively and prints detailed debug info at each step.
"""
import asyncio
import sys
from playwright.async_api import async_playwright

# Target village
DISTRICT_CODE = "3"      # କଟକ
TAHASIL_CODE = "5"       # ନରସିଂହପୁର  
VILLAGE_CODE = "180"     # ବାଘଧରିଆ

BASE_URL = "http://bhulekh.ori.nic.in"
ROR_URL = f"{BASE_URL}/RoRViewBhunakshaOdisha.aspx"

SELECTOR_DISTRICT = 'select#ctl00_ContentPlaceHolder1_ddlDistrict, select[id*="ddlDistrict"]'
SELECTOR_TAHASIL = 'select#ctl00_ContentPlaceHolder1_ddlTahsil, select[id*="ddlTahsil"]'
SELECTOR_VILLAGE = 'select#ctl00_ContentPlaceHolder1_ddlRI, select[id*="ddlRI"]'
SELECTOR_KHATIYAN = 'select#ctl00_ContentPlaceHolder1_ddlBindData, select[id*="ddlBindData"]'
SELECTOR_RADIO_KHATIYAN = 'input#ctl00_ContentPlaceHolder1_rbtnRORSearchtype_0'


async def diagnose():
    print("=" * 60)
    print("VILLAGE DIAGNOSTIC SCRIPT")
    print(f"District: {DISTRICT_CODE}, Tahasil: {TAHASIL_CODE}, Village: {VILLAGE_CODE}")
    print("=" * 60)
    
    async with async_playwright() as p:
        print("\n[1] Launching browser...")
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        
        try:
            # Navigate to RoR page
            print(f"\n[2] Navigating to {ROR_URL}...")
            await page.goto(ROR_URL, wait_until='domcontentloaded', timeout=60000)
            await asyncio.sleep(2)
            
            # Wait for district dropdown
            print("\n[3] Waiting for district dropdown...")
            await page.wait_for_selector(SELECTOR_DISTRICT, timeout=30000)
            print("    District dropdown found!")
            
            # Select district
            print(f"\n[4] Selecting district {DISTRICT_CODE}...")
            await page.select_option(SELECTOR_DISTRICT, DISTRICT_CODE)
            await asyncio.sleep(2)
            
            # Select Khatiyan search type
            print("\n[5] Selecting Khatiyan search type...")
            radio = page.locator(SELECTOR_RADIO_KHATIYAN)
            if await radio.count() > 0:
                await radio.click()
                await asyncio.sleep(1)
                print("    Khatiyan radio selected!")
            else:
                print("    WARNING: Khatiyan radio not found")
            
            # Wait for tahasil dropdown
            print("\n[6] Waiting for tahasil dropdown to populate...")
            for i in range(30):
                options = await page.locator(f"{SELECTOR_TAHASIL} option").count()
                if options > 1:
                    print(f"    Tahasil dropdown has {options} options")
                    break
                await asyncio.sleep(1)
            else:
                print("    ERROR: Tahasil dropdown didn't populate!")
                return
            
            # Select tahasil
            print(f"\n[7] Selecting tahasil {TAHASIL_CODE}...")
            await page.select_option(SELECTOR_TAHASIL, TAHASIL_CODE)
            await asyncio.sleep(2)
            
            # Wait for village dropdown
            print("\n[8] Waiting for village dropdown to populate...")
            for i in range(30):
                options = await page.locator(f"{SELECTOR_VILLAGE} option").count()
                if options > 1:
                    print(f"    Village dropdown has {options} options")
                    break
                await asyncio.sleep(1)
            else:
                print("    ERROR: Village dropdown didn't populate!")
                return
            
            # Select village
            print(f"\n[9] Selecting village {VILLAGE_CODE}...")
            await page.select_option(SELECTOR_VILLAGE, VILLAGE_CODE)
            await asyncio.sleep(2)
            
            # Wait for khatiyan dropdown
            print("\n[10] Waiting for khatiyan dropdown to populate...")
            for i in range(30):
                options = await page.locator(f"{SELECTOR_KHATIYAN} option").count()
                if options > 1:
                    print(f"    Khatiyan dropdown has {options} options")
                    break
                await asyncio.sleep(1)
            else:
                print("    ERROR: Khatiyan dropdown didn't populate!")
                # Save screenshot
                await page.screenshot(path="diagnose_khatiyan_empty.png")
                print("    Screenshot saved: diagnose_khatiyan_empty.png")
                return
            
            # Get all khatiyan options
            print("\n[11] Getting khatiyan options...")
            khatiyans = await page.evaluate("""
                () => {
                    const select = document.querySelector('select[id*="ddlBindData"]');
                    if (!select) return [];
                    return Array.from(select.options).map(o => ({
                        value: o.value,
                        text: o.text,
                        valueLen: o.value.length,
                        textLen: o.text.length
                    }));
                }
            """)
            
            print(f"\n    Found {len(khatiyans)} khatiyans:")
            print("-" * 60)
            for i, kh in enumerate(khatiyans[:10]):  # First 10
                print(f"    [{i}] value='{kh['value']}' (len={kh['valueLen']}) | text='{kh['text']}' (len={kh['textLen']})")
            if len(khatiyans) > 10:
                print(f"    ... and {len(khatiyans) - 10} more")
            print("-" * 60)
            
            # Try selecting first few khatiyans
            print("\n[12] Testing khatiyan selection (first 3)...")
            for i, kh in enumerate(khatiyans[1:4]):  # Skip empty first option, try next 3
                print(f"\n    Attempting to select khatiyan: '{kh['value']}'...")
                try:
                    await asyncio.wait_for(
                        page.select_option(SELECTOR_KHATIYAN, kh['value']),
                        timeout=10
                    )
                    print(f"    SUCCESS: Selected '{kh['value']}'")
                    
                    # Try clicking View RoR
                    print(f"    Clicking View RoR button...")
                    btn = page.locator('input#ctl00_ContentPlaceHolder1_btnViewRecord, input[value*="View"]')
                    if await btn.count() > 0:
                        await asyncio.wait_for(btn.click(), timeout=10)
                        print(f"    SUCCESS: Clicked View RoR")
                        await asyncio.sleep(3)
                        
                        # Check page content
                        title = await page.title()
                        url = page.url
                        print(f"    Page title: {title}")
                        print(f"    URL: {url}")
                        
                        # Save screenshot
                        screenshot_name = f"diagnose_khatiyan_{i+1}.png"
                        await page.screenshot(path=screenshot_name)
                        print(f"    Screenshot saved: {screenshot_name}")
                        
                        # Go back
                        back_btn = page.locator('input#btnKhatiyan, input[value*="Khatiyan Page"]')
                        if await back_btn.count() > 0:
                            await back_btn.click()
                            await asyncio.sleep(2)
                    else:
                        print(f"    WARNING: View RoR button not found")
                        
                except asyncio.TimeoutError:
                    print(f"    TIMEOUT: Selection hung after 10s")
                    await page.screenshot(path=f"diagnose_timeout_{i+1}.png")
                    print(f"    Screenshot saved: diagnose_timeout_{i+1}.png")
                except Exception as e:
                    print(f"    ERROR: {e}")
            
            print("\n" + "=" * 60)
            print("DIAGNOSTIC COMPLETE")
            print("=" * 60)
            
        except Exception as e:
            print(f"\nFATAL ERROR: {e}")
            await page.screenshot(path="diagnose_error.png")
            print("Screenshot saved: diagnose_error.png")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(diagnose())
