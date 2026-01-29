# PyInstaller spec for Bhulekh Scraper — Browser Setup
# Build: pyinstaller install_browsers.spec
# Run once: dist/install_browsers.exe (downloads Chromium so bhulekh_scraper.exe can run)

a = Analysis(
    ['install_browsers.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'playwright._impl._api_types',
        'playwright.sync_api',
        'playwright._impl._driver',
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
    name='install_browsers',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
