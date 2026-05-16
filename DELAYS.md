# Delays and sleeps in the scraper

All of these add time per khatiyan or per navigation. Use **`--fast`** to scale them down by ~6.7× (delay_scale=0.15) for maximum speed. The site may timeout or block if you go too fast.

## Where delays happen

| Location | Normal delay | With `--fast` (×0.15) | When |
|----------|--------------|------------------------|------|
| **human_delay()** | Various (see below) | All scaled | Used in many places |
| **wait_for_page_load()** | networkidle + 0.2–0.4 s | + ~0.03–0.06 s | After every page load |
| **select_dropdown()** | hover 0.15–0.35, then 0.2–0.4, then after load 0.3–0.6 | scaled | District/Tahasil/Village/Khatiyan select |
| **wait_for_dropdown_populated()** | Poll every 0.25 s | Poll every 0.05 s | Until dropdown has options |
| **select_search_type()** | 0.3–0.6 (if disabled), hover 0.15–0.35 | scaled | Once per district |
| **navigate_to_ror_page()** | 0.4–0.8, mouse 0.2–0.5, 0.3–0.6; retry 1–2 s, 0.8–1.5 | scaled | Start + on timeout retry |
| **click_view_ror()** | hover 0.15–0.4 | scaled | **Every khatiyan** |
| **click_khatiyan_page()** | hover 0.15–0.35, then 0.3–0.6 | scaled | **Every khatiyan** (back button) |
| **process_khatiyan()** | 0.3–0.6 after select, 0.4–0.7 after load, 0.3–0.6 after back; retry 0.5–1, 0.8–1.5 | scaled | **Per khatiyan** |
| **process_village()** | 0.2–0.4 after dropdown, **0.4–0.8 between khatiyans**, 0.8 s after re-select village | scaled | Per village / per khatiyan |
| **process_tahasil()** | 0.2–0.4, **0.5–1.0 between villages**, 0.8 s after re-select tahasil | scaled | Per tahasil / per village |
| **process_district()** | 0.2–0.4, **0.8–1.2 between tahasils** | scaled | Per district |
| **run()** | 0.5–1.0 at start, **1.0–2.0 between districts** | scaled | Once + per district |
| **cleanup()** | 0.2 s | unchanged | On exit |

Rough **per-khatiyan** extra delay (normal): ~0.15+0.3+0.2+0.4+0.7+0.3+0.6 + 0.4–0.8 (between) + wait_for_page_load (×2) → about **2–4+ seconds** of artificial delay per khatiyan on top of network and page load. With `--fast`, that becomes about **0.3–0.7 s** extra.

## How to use

```bash
# Normal (safe for site)
python bhulekh_scraper.py --data-dir bhulekh_data --resume

# Minimal delays (faster; risk of timeouts/blocks)
python bhulekh_scraper.py --data-dir bhulekh_data --resume --fast
python run_workers.py --workers 50 --data-dir bhulekh_data --resume --fast
```
