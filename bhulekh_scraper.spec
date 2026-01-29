# PyInstaller spec for Bhulekh Scraper
# Build: pyinstaller bhulekh_scraper.spec
# Output: dist/bhulekh_scraper.exe (one-file) or dist/bhulekh_scraper/ (one-folder)

# Switch to one-folder for faster startup: change onefile to False and run again
onefile = True

a = Analysis(
    ['bhulekh_scraper.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'playwright._impl._api_types',
        'playwright.async_api',
        'playwright.sync_api',
        'pandas',
        'numpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='bhulekh_scraper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,   # CLI app: show console for --help, logs, etc.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
