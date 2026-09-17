import sys, pathlib, tempfile, shutil, json
import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_mask2_"))

import core
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets, QtCore, QtGui
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

# 実機のデスクトップ上に本物のモーダルダイアログが出て自動テストが
# 止まってしまわないよう、情報/警告ダイアログはログ出力だけにする
QtWidgets.QMessageBox.information = staticmethod(
    lambda *a, **kw: print("  [QMessageBox.information]", a[1:3]))
QtWidgets.QMessageBox.warning = staticmethod(
    lambda *a, **kw: print("  [QMessageBox.warning]", a[1:3]))

NAME = "mask_test2"
d = core.recipe_dir(NAME)

# 全体スクリーンショット 200x300 (w=200,h=300)、元の切り抜きは
# (cx=100,cy=50) 中心の 20x10 (すでに一部除外されたマスク付き)。
# R=G=Bのグレー画像にしておけば、RGB/BGRの並び順を気にせず
# グレースケール変換後の画素値を単純に比較できる
FULL_W, FULL_H = 200, 300
full_gray_src = np.random.RandomState(2).randint(0, 256, (FULL_H, FULL_W), dtype=np.uint8)
full_rgb = np.stack([full_gray_src] * 3, axis=-1)
Image.fromarray(full_rgb).save(d / "ctx1.png")

ow, oh = 20, 10
cx, cy = 100, 50
ox1, oy1 = cx - ow // 2, cy - oh // 2
orig_tpl = np.random.RandomState(3).randint(0, 256, (oh, ow), dtype=np.uint8)
orig_mask = np.full((oh, ow), 255, dtype=np.uint8)
orig_mask[:, :5] = 0  # 左5列はすでに除外済み(実測で不安定だった想定)
Image.fromarray(orig_tpl).save(d / "s1.png")
Image.fromarray(orig_mask).save(d / "mask1.png")

steps = [{"type": "tap", "label": "S1", "template": "s1.png", "mask": "mask1.png",
          "context": "ctx1.png", "x": cx, "y": cy, "dx": 0, "dy": 0,
          "method": "masked_zncc", "threshold": 0.9}]
core.save_recipe(NAME, {"device_size": [FULL_W, FULL_H], "screenshot_size": [FULL_W, FULL_H],
                         "popups": [], "steps": steps})

dlg = gui.MaskEditorDialog(NAME, "steps", (0,))
assert dlg.full_tpl.shape == (FULL_H, FULL_W), dlg.full_tpl.shape
assert dlg.active_rect == (ox1, oy1, ox1 + ow, oy1 + oh), dlg.active_rect
# 元の切り抜き範囲の外側は全域有効、内側は実測マスク(左5列だけ除外)を継承
assert (dlg.mask[0:oy1, :] > 0).all(), "外側は初期状態で全域有効のはず"
assert (dlg.mask[oy1:oy1 + oh, ox1:ox1 + 5] == 0).all(), "内側の左5列は実測で除外されていたはず"
assert (dlg.mask[oy1:oy1 + oh, ox1 + 5:ox1 + ow] > 0).all(), "内側の残りは有効のはず"
print("初期状態: OK (外側=全域有効、内側は実測マスクを継承)")

# --- 元の範囲を"内側"にとどめて絞り込む(narrowing、従来通りの操作) ---
scale = dlg.preview_scale
dlg.on_drag(int((ox1 + 8) * scale), int((oy1 + 1) * scale),
            int((ox1 + 18) * scale), int((oy1 + 9) * scale))
x1, y1, x2, y2 = dlg.active_rect
assert (x1, y1, x2, y2) == (ox1 + 8, oy1 + 1, ox1 + 18, oy1 + 9)
print("内側での絞り込み: OK ->", dlg.active_rect)

# --- 元に戻す ---
dlg.on_reset()
assert dlg.active_rect == (ox1, oy1, ox1 + ow, oy1 + oh)
print("元に戻す: OK")

# --- 全体スクリーンショットの、元の切り抜き範囲の"外側"で新しく選び直す ---
new_x1, new_y1, new_x2, new_y2 = 10, 10, 40, 30
dlg.on_drag(int(new_x1 * scale), int(new_y1 * scale), int(new_x2 * scale), int(new_y2 * scale))
assert dlg.active_rect == (new_x1, new_y1, new_x2, new_y2)
sub_mask = dlg.mask[new_y1:new_y2, new_x1:new_x2]
assert (sub_mask > 0).all(), "元の範囲の外側なので全域有効のはず"
print("外側での新規選択: OK ->", dlg.active_rect, "(全域有効)")

dlg.sp_threshold.setValue(0.6)
dlg.save()

saved = json.loads((d / "recipe.json").read_text(encoding="utf-8"))
saved_step = saved["steps"][0]
assert saved_step["x"] == (new_x1 + new_x2) // 2
assert saved_step["y"] == (new_y1 + new_y2) // 2
assert saved_step["dx"] == 0 and saved_step["dy"] == 0
assert saved_step["threshold"] == 0.6
saved_tpl = np.array(Image.open(d / saved_step["template"]))
saved_mask = np.array(Image.open(d / saved_step["mask"]))
assert saved_tpl.shape == (new_y2 - new_y1, new_x2 - new_x1)
assert saved_mask.shape == (new_y2 - new_y1, new_x2 - new_x1)
assert (saved_mask > 0).all()
# 保存されたテンプレートの画素値が、全体スクリーンショットのその位置の
# 画素値と一致する(=単一フレームのcontextから正しく切り出せている)
full_gray_check = np.array(Image.open(d / "ctx1.png").convert("L"))
assert np.array_equal(saved_tpl, full_gray_check[new_y1:new_y2, new_x1:new_x2])
print("保存内容(外側選択時): OK -> x,y,dx,dy,threshold,template/maskサイズ、画素値すべて正しい")

print("\n=== MaskEditorDialog (full-screen version): ALL CHECKS PASSED ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)
