# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('uiautomator2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('adbutils')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('cv2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# windows-toasts(トースト通知)とその依存であるwinrt(pywinrt)。winrtは
# .pyd拡張モジュールを名前空間パッケージ直下に置く構成で、collect_all無しだと
# PyInstallerの解決に失敗し「開発環境では動くがexeでは無反応」になることを
# 実機ビルドで確認したため、両方ともcollect_allで明示的に同梱する
tmp_ret = collect_all('windows_toasts')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('winrt')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='TapReplay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TapReplay',
)
# THIRD_PARTY_LICENSES.txt(同梱している第三者バイナリのライセンス表示)は、
# ここでdatas/COLLECTに足す方式だとPyInstaller 6のインクリメンタルビルドで
# 収録先が_internal配下になったり、そもそも収録されなかったりと実行のたびに
# 結果が変わることを実機ビルドで確認した。exeと同じ最上位に確実に置くため、
# build.bat / watch_build.ps1側でこのビルドの後にコピーする(スクリプトを
# 参照)。ここでは追加しない
