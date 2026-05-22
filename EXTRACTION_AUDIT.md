# Data Extraction Audit - All Possible Failure Scenarios

## Overview

This document lists ALL scenarios where data extraction can fail or be incomplete.

---

## 1. PAGE LOADING ISSUES

### 1.1 Page Not Fully Loaded
- **Scenario**: We start extraction before page content is rendered
- **Current handling**: Wait for `#gvfront`, `#gvRorBack`, etc.
- **Potential gap**: Wait uses OR logic - if front table loads, we proceed even if back table isn't ready
- **Status**: ✅ FIXED - Added scroll + explicit wait for back table

### 1.2 Lazy Loading
- **Scenario**: Back page table (plots) loads only when scrolled into view
- **Current handling**: Added scroll to bottom before extraction
- **Potential gap**: Some pages might need multiple scrolls or specific element focus
- **Status**: ✅ FIXED - Added scroll + wait

### 1.3 JavaScript Not Executed
- **Scenario**: ASP.NET postback not completed, dynamic content not loaded
- **Current handling**: Wait for network idle and specific selectors
- **Potential gap**: Very slow pages might timeout before JS completes
- **Status**: ⚠️ NEEDS VERIFICATION

### 1.4 Timeout Before Content
- **Scenario**: Page takes >15 seconds to load
- **Current handling**: 15 second timeout, then proceed anyway
- **Potential gap**: Should retry on timeout instead of proceeding with empty data
- **Status**: ❌ NEEDS FIX

---

## 2. HTML STRUCTURE VARIATIONS

### 2.1 Type 1 vs Type 2 Pages
- **Scenario**: Different page layouts use different element IDs
- **Current handling**: Detect type and use appropriate extractor
- **Potential gap**: Detection might fail, wrong extractor used
- **Status**: ⚠️ NEEDS VERIFICATION

### 2.2 Unknown Page Types (Type 3, 4, etc.)
- **Scenario**: Some districts/villages use completely different layouts
- **Current handling**: Only Type 1 and Type 2 are handled
- **Potential gap**: Unknown types return empty data
- **Status**: ❌ UNKNOWN - Need to check if other types exist

### 2.3 Element ID Variations
- **Scenario**: Same field has different IDs across districts
  - `gvfront_ctl02_lblMouja` vs `gvRorFront_ctl02_lblMouja` vs `ctl00_ContentPlaceHolder1_lblMouja`
- **Current handling**: Multiple selector fallbacks
- **Potential gap**: Some variations might not be covered
- **Status**: ⚠️ NEEDS VERIFICATION

### 2.4 Table ID Variations for Plots
- **Scenario**: Plot table uses different IDs
  - `gvRorBack`, `gvRorBack2`, `gvplotdetail`, `gvRorFrontBack`
- **Current handling**: Try multiple table IDs
- **Potential gap**: Some tables might use completely different IDs
- **Status**: ⚠️ NEEDS VERIFICATION

---

## 3. DATA EXTRACTION ISSUES

### 3.1 Plot Row Detection
- **Scenario**: Plot rows not detected correctly
- **Current handling**: Look for rows with `a[id*="lblPlotNo"]` or `span[id*="lblPlotNo"]`
- **Potential gap**: Some plots might use different element patterns
- **Status**: ⚠️ NEEDS VERIFICATION

### 3.2 Plot Field Selectors
- **Scenario**: Plot fields (acre, decimil, hector) have different selectors
- **Current handling**: Multiple selector patterns per field
- **Potential gap**: Some variations not covered
- **Status**: ⚠️ NEEDS VERIFICATION

### 3.3 Empty Elements vs Missing Elements
- **Scenario**: Element exists but is empty vs element doesn't exist
- **Current handling**: Check `count() > 0` before reading
- **Potential gap**: Element might exist but be hidden or have whitespace only
- **Status**: ✅ OK

### 3.4 Pagination
- **Scenario**: Plots are paginated, only first page extracted
- **Current handling**: None
- **Potential gap**: If pagination exists, we only get first page
- **Status**: ❓ UNKNOWN - Need to check if pagination exists

### 3.5 Nested Tables
- **Scenario**: Plot data in nested tables
- **Current handling**: Only look at direct rows
- **Potential gap**: Nested structure might be missed
- **Status**: ⚠️ NEEDS VERIFICATION

### 3.6 iFrames
- **Scenario**: Data in an iframe
- **Current handling**: None
- **Potential gap**: If iframe exists, data not extracted
- **Status**: ❓ UNKNOWN - Need to check if iframes are used

---

## 4. NAVIGATION ISSUES

### 4.1 Dropdown Selection Fails
- **Scenario**: Dropdown value has trailing spaces or special characters
- **Current handling**: Use label-based selection as fallback
- **Potential gap**: Both value and label might fail
- **Status**: ✅ FIXED - Label-based selection with 15s timeout

### 4.2 View Button Doesn't Work
- **Scenario**: Click on View button doesn't trigger page load
- **Current handling**: Retry logic with backoff
- **Potential gap**: If button is disabled or has different ID
- **Status**: ⚠️ NEEDS VERIFICATION

### 4.3 Back Button Fails
- **Scenario**: Can't navigate back to khatiyan list
- **Current handling**: Try multiple back button selectors
- **Potential gap**: Might need to reload page
- **Status**: ✅ OK - Has fallback to navigate to RoR page

---

## 5. DATA STORAGE ISSUES

### 5.1 JSON Encoding
- **Scenario**: Special characters cause JSON encoding to fail
- **Current handling**: `ensure_ascii=False`
- **Potential gap**: Some control characters might cause issues
- **Status**: ✅ OK

### 5.2 Database Locked
- **Scenario**: SQLite database locked by another process
- **Current handling**: None specific
- **Potential gap**: Might lose data on write failure
- **Status**: ⚠️ NEEDS REVIEW

### 5.3 Partial Write
- **Scenario**: Write interrupted, data corrupted
- **Current handling**: Commit after each khatiyan
- **Potential gap**: Power loss during commit
- **Status**: ✅ OK - SQLite handles this

---

## 6. WEBSITE-SPECIFIC ISSUES

### 6.1 Session Timeout
- **Scenario**: Website session expires during scraping
- **Current handling**: Detect and reinitialize
- **Potential gap**: Might not detect all session timeout scenarios
- **Status**: ⚠️ NEEDS VERIFICATION

### 6.2 Captcha
- **Scenario**: Website shows captcha
- **Current handling**: Detect and wait/alert
- **Potential gap**: Might proceed without solving captcha
- **Status**: ⚠️ NEEDS VERIFICATION

### 6.3 Server Errors
- **Scenario**: Website returns 500/503 errors
- **Current handling**: Retry with backoff
- **Potential gap**: Might not detect all error states
- **Status**: ✅ OK

### 6.4 Rate Limiting
- **Scenario**: Website blocks due to too many requests
- **Current handling**: Human-like delays between requests
- **Potential gap**: Might still trigger rate limits
- **Status**: ✅ OK

---

## 7. LEGITIMATE EMPTY DATA

### 7.1 Transferred Khatiyans
- **Scenario**: Land transferred to another khatiyan, no plots remain
- **Reality**: This is LEGITIMATE - these records genuinely have no plots
- **Current handling**: Store empty plots array
- **Status**: ✅ OK - Not a bug

### 7.2 Cancelled/Void Records
- **Scenario**: Record is cancelled or void
- **Reality**: This is LEGITIMATE
- **Status**: ✅ OK - Not a bug

---

## VERIFICATION CHECKLIST

Before declaring extraction "working", verify:

1. [ ] Run `verify_extraction.py` on 10+ random khatiyans
2. [ ] Run `verify_extraction.py --empty-only` on 10+ khatiyans with empty plots
3. [ ] Check if discrepancies exist between page content and stored data
4. [ ] Confirm no unknown page types (Type 3, 4, etc.)
5. [ ] Confirm no pagination on plot tables
6. [ ] Confirm no iframes used
7. [ ] Compare plot counts: page estimate vs stored vs fresh extraction

---

## QUICK FIX LIST

### High Priority (Data Loss)
1. **Timeout handling**: Don't proceed with empty data on timeout - retry or fail
2. **Unknown page types**: Log and capture HTML for any unrecognized layouts

### Medium Priority (Data Quality)  
3. **More table ID patterns**: Add any new patterns found during verification
4. **Better logging**: Log when plots extracted vs when empty

### Low Priority (Edge Cases)
5. **Pagination handling**: If pagination exists, implement page iteration
6. **iFrame handling**: If iframes exist, switch to iframe context

---

## COMMANDS TO VERIFY

```bash
# On AWS, run verification
cd ~/justokay && git pull

# Test on 10 random khatiyans
uv run python verify_extraction.py --data-dir bhulekh_data --samples 10

# Test on 10 khatiyans with empty plots specifically  
uv run python verify_extraction.py --data-dir bhulekh_data --samples 10 --empty-only

# Check output
ls -la verification_output/
cat verification_output/verification_report.json | jq '.with_discrepancies'
```
