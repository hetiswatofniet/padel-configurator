# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-specificatie: één zelfstandig programma, ook zonder Python."""

a = Analysis(
    ["configureer.py"],
    pathex=[],
    binaries=[],
    datas=[("Starthier.html", "."), ("template_book_padel.py", ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "email_validator"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="padel-configurator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
