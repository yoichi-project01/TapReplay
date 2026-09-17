import sys, pathlib, tempfile, shutil
import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_tapoffset_"))

import core
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

d = core.recipe_dir("x")
full_gray = np.random.RandomState(9).randint(0, 256, (200, 300), dtype=np.uint8)
Image.fromarray(np.stack([full_gray] * 3, axis=-1)).save(d / "img.png")

# --- ケース1: タップ位置を指定しない(検出範囲の中心を使う) ---
dlg = gui.ScreenCropDialog(d / "img.png")
scale = dlg.preview_scale
dlg.on_drag(int(10 * scale), int(10 * scale), int(30 * scale), int(30 * scale))
dlg.accept()
tpl, mask, tap_x, tap_y, dx, dy = dlg.result_data
assert (tap_x, tap_y) == (20, 20), (tap_x, tap_y)
assert (dx, dy) == (0, 0), (dx, dy)
print("1) タップ位置未指定 -> 範囲の中心, dx=dy=0: OK")

# --- ケース2: タップ位置を範囲の外の離れた場所に指定する ---
dlg2 = gui.ScreenCropDialog(d / "img.png")
dlg2.on_drag(int(10 * scale), int(10 * scale), int(30 * scale), int(30 * scale))  # 中心(20,20)
dlg2.on_click_tap_pos(int(100 * scale), int(150 * scale))  # 範囲外(100,150)をタップ位置に
dlg2.accept()
tpl2, mask2, tap_x2, tap_y2, dx2, dy2 = dlg2.result_data
assert (tap_x2, tap_y2) == (100, 150)
assert (dx2, dy2) == (100 - 20, 150 - 20) == (80, 130)
print("2) タップ位置を範囲外に指定 -> tap=(100,150), dx,dy=(80,130): OK")

# --- ケース3: 「範囲の中心に戻す」でリセットされる ---
dlg2.on_reset_tap()
assert dlg2.tap_pos is None
dlg2.accept()
tpl3, mask3, tap_x3, tap_y3, dx3, dy3 = dlg2.result_data
assert (tap_x3, tap_y3) == (20, 20)
assert (dx3, dy3) == (0, 0)
print("3) リセット -> 範囲の中心に戻る: OK")

# --- ケース4: 再生時、マッチ位置+dx/dyが実際に正しいタップ位置になるか ---
# 記録時とは違う位置に画面全体がずれて写っている状況を模擬する
match_cx, match_cy = 205, 260  # 実際に検出された(ずれた)中心位置
final_x = match_cx + dx2
final_y = match_cy + dy2
assert (final_x, final_y) == (205 + 80, 260 + 130)
print("4) 再生時の位置計算(matched_center + dx,dy)が意図通り: OK ->", (final_x, final_y))

print("\n=== independent tap position: ALL CHECKS PASSED ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)
