import sys, pathlib, tempfile, shutil, json
import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_mask_tappos_"))

import core
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets, QtCore, QtGui
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

QtWidgets.QMessageBox.information = staticmethod(
    lambda *a, **kw: print("  [info]", a[1:3]))
QtWidgets.QMessageBox.warning = staticmethod(
    lambda *a, **kw: print("  [warning]", a[1:3]))

NAME = "mask_tappos_test"
d = core.recipe_dir(NAME)

FULL_W, FULL_H = 200, 300
full_gray_src = np.random.RandomState(11).randint(0, 256, (FULL_H, FULL_W), dtype=np.uint8)
Image.fromarray(np.stack([full_gray_src] * 3, axis=-1)).save(d / "ctx1.png")

ow, oh = 20, 10
cx, cy = 100, 50
ox1, oy1 = cx - ow // 2, cy - oh // 2
orig_tpl = np.random.RandomState(3).randint(0, 256, (oh, ow), dtype=np.uint8)
orig_mask = np.full((oh, ow), 255, dtype=np.uint8)
Image.fromarray(orig_tpl).save(d / "s1.png")
Image.fromarray(orig_mask).save(d / "mask1.png")

# --- ケース0: 既存ノードはdx=dy=0(=タップ位置は範囲の中心)のまま ---
steps = [{"type": "tap", "label": "S1", "template": "s1.png", "mask": "mask1.png",
          "context": "ctx1.png", "x": cx, "y": cy, "dx": 0, "dy": 0,
          "method": "masked_zncc", "threshold": 0.9}]
core.save_recipe(NAME, {"device_size": [FULL_W, FULL_H], "screenshot_size": [FULL_W, FULL_H],
                         "popups": [], "steps": steps})

dlg = gui.MaskEditorDialog(NAME, "steps", (0,))
assert dlg.tap_pos is None, dlg.tap_pos
print("0) dx=dy=0のノード -> tap_pos=None(範囲の中心を使う既定動作): OK")

# --- ケース1: 範囲をドラッグで絞り込んでも、タップ位置は未指定のまま保存 ---
scale = dlg.preview_scale
dlg.on_drag(int((ox1 + 8) * scale), int((oy1 + 1) * scale),
            int((ox1 + 18) * scale), int((oy1 + 9) * scale))
x1, y1, x2, y2 = dlg.active_rect
dlg.save()
saved = json.loads((d / "recipe.json").read_text(encoding="utf-8"))["steps"][0]
assert saved["x"] == (x1 + x2) // 2 and saved["y"] == (y1 + y2) // 2
assert saved["dx"] == 0 and saved["dy"] == 0
print("1) タップ位置未指定のまま範囲だけ絞り込み -> 中心をタップ, dx=dy=0: OK")

# --- ケース2: クリックしてタップ位置を範囲の外の離れた場所に指定する ---
dlg2 = gui.MaskEditorDialog(NAME, "steps", (0,))
rx1, ry1, rx2, ry2 = dlg2.active_rect
rcx, rcy = (rx1 + rx2) // 2, (ry1 + ry2) // 2
click_x, click_y = rcx + 15, ry2 + 20  # 範囲の外側の離れた点
dlg2.on_click_tap_pos(int(click_x * scale), int(click_y * scale))
assert dlg2.tap_pos == (click_x, click_y), dlg2.tap_pos
dlg2.save()
saved2 = json.loads((d / "recipe.json").read_text(encoding="utf-8"))["steps"][0]
assert saved2["x"] == click_x and saved2["y"] == click_y, saved2
assert saved2["dx"] == click_x - rcx and saved2["dy"] == click_y - rcy, saved2
print("2) タップ位置を範囲外に指定 -> x,y=クリック位置, dx,dy=中心からのずれ: OK ->",
      (saved2["x"], saved2["y"], saved2["dx"], saved2["dy"]))

# --- ケース3: 再度開いたときに、保存したdx/dyから同じタップ位置を復元する ---
dlg3 = gui.MaskEditorDialog(NAME, "steps", (0,))
assert dlg3.active_rect == dlg2.active_rect, (dlg3.active_rect, dlg2.active_rect)
assert dlg3.tap_pos == (click_x, click_y), dlg3.tap_pos
print("3) 再オープン時にdx/dyからタップ位置を正しく復元: OK ->", dlg3.tap_pos)

# --- ケース4: 「範囲の中心に戻す」でタップ位置指定が解除される ---
dlg3.on_reset_tap()
assert dlg3.tap_pos is None
dlg3.save()
saved3 = json.loads((d / "recipe.json").read_text(encoding="utf-8"))["steps"][0]
rx1, ry1, rx2, ry2 = dlg3.active_rect
assert saved3["x"] == (rx1 + rx2) // 2 and saved3["y"] == (ry1 + ry2) // 2
assert saved3["dx"] == 0 and saved3["dy"] == 0
print("4) タップ位置を中心に戻す -> dx=dy=0で保存: OK")

# --- ケース5: 「元に戻す」(on_reset)で、マスク/範囲だけでなくタップ位置も
#     読み込み時点の状態(dlg4作成時点でのdx/dyから復元した値)に戻る ---
# まず離れたタップ位置を設定・保存してから再オープンし、範囲とタップ位置を
# 両方変更し、on_reset()でロード時点の状態に戻ることを確認する
dlg4 = gui.MaskEditorDialog(NAME, "steps", (0,))  # ロード時点: tap_pos=None(ケース4の結果)
assert dlg4.tap_pos is None
dlg4.on_drag(int(10 * scale), int(10 * scale), int(40 * scale), int(30 * scale))
dlg4.on_click_tap_pos(int(60 * scale), int(60 * scale))
assert dlg4.tap_pos == (60, 60)
dlg4.on_reset()
assert dlg4.tap_pos is None, dlg4.tap_pos
assert dlg4.active_rect == dlg4._orig_active_rect
print("5) 元に戻す(on_reset) -> タップ位置もロード時点の状態に復元: OK")

print("\n=== MaskEditorDialog independent tap position: ALL CHECKS PASSED ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)
