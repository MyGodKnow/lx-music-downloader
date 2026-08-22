# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['lx_music_downloader.py'],
    pathex=[],
    binaries=[('C:/Users/God Know/.workbuddy/binaries/node/versions/22.22.2/node.exe', 'node'),
              ('C:/Program Files/格式工厂/ffmpeg.exe', 'ffmpeg')],
    datas=[('sources', 'sources'), ('lx_url_server.js', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LXMusicDownloader',
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
