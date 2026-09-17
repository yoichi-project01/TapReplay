import sys, pathlib, tempfile, shutil, json
import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_fromfail_"))

import core
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets, QtCore, QtGui
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

QtWidgets.QMessageBox.information = staticmethod(lambda *a, **kw: None)
QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **kw: print("  [warning]", a[1:3]))

NAME = "fromfail_test"
d = core.recipe_dir(NAME)

FULL_W, FULL_H = 150, 200
full_gray = np.random.RandomState(5).randint(0, 256, (FULL_H, FULL_W), dtype=np.uint8)
Image.fromarray(np.stack([full_gray] * 3, axis=-1)).save(d / "error_120000_test.png")

steps = [{"type": "tap", "label": "A", "template": "error_120000_test.png",
          "context": "error_120000_test.png", "x": 10, "y": 10, "dx": 0, "dy": 0,
          "method": "ccoeff", "threshold": 0.85}]
core.save_recipe(NAME, {"device_size": [FULL_W, FULL_H], "screenshot_size": [FULL_W, FULL_H],
                         "popups": [], "steps": steps})
core.append_failure(NAME, "A", "テスト失敗理由", "error_120000_test.png", attempts=None)

# --- 1) ScreenCropDialog: 失敗画像から範囲を選ぶ ---
dlg = gui.ScreenCropDialog(d / "error_120000_test.png", "test")
scale = dlg.preview_scale
dlg.on_drag(int(5 * scale), int(5 * scale), int(25 * scale), int(15 * scale))
assert dlg.rect == (5, 5, 25, 15)
dlg.accept()
tpl, mask, cx, cy, dx, dy = dlg.result_data
assert tpl.shape == (10, 20)
assert (mask > 0).all()
assert cx == 15 and cy == 10
assert np.array_equal(tpl, full_gray[5:15, 5:25])
print("1) ScreenCropDialog crop: OK ->", tpl.shape, (cx, cy))

# --- 2) 「新しいステップを作る」相当のロジックを直接検証 ---
data = json.loads((d / "recipe.json").read_text(encoding="utf-8"))
before_count = len(data["steps"])
tpl_name = "step_from_failure_test.png"
mask_name = "step_from_failure_test_mask.png"
core.imwrite(d / tpl_name, tpl)
core.imwrite(d / mask_name, mask)
new_node = {"type": "tap", "label": "新ステップ", "template": tpl_name, "mask": mask_name,
            "context": "error_120000_test.png", "x": cx, "y": cy, "dx": 0, "dy": 0,
            "method": "masked_zncc", "threshold": 0.85}
data["steps"].append(new_node)
core.save_recipe(NAME, data)
reloaded = core.load_recipe(NAME)
assert len(reloaded["steps"]) == before_count + 1
assert reloaded["steps"][-1]["label"] == "新ステップ"
assert reloaded["steps"][-1]["_gray"] is not None
print("2) 新規ステップ追加 -> core.load_recipeで正常に読み込める: OK")

# --- 3) ConditionPickerDialogの「失敗履歴の画像から選ぶ」 ---
cp_dlg = gui.ConditionPickerDialog(d, reloaded["steps"])
# on_pick_from_failure内のQInputDialog.getItemとScreenCropDialog.pickをモック
orig_get_item = QtWidgets.QInputDialog.getItem
orig_screencrop_pick = gui.ScreenCropDialog.pick
QtWidgets.QInputDialog.getItem = staticmethod(lambda *a, **kw: (a[3][0], True))
QtWidgets.QInputDialog.getItem = staticmethod(
    lambda self_, title, label, items, cur, editable: (items[0], True))
gui.ScreenCropDialog.pick = staticmethod(
    lambda image_path, title="x", message=None, parent=None: (
        np.zeros((5, 5), dtype=np.uint8), np.full((5, 5), 255, dtype=np.uint8), 3, 3, 0, 0))
cp_dlg.on_pick_from_failure()
QtWidgets.QInputDialog.getItem = orig_get_item
gui.ScreenCropDialog.pick = orig_screencrop_pick

assert cp_dlg._extra_result is not None
result = cp_dlg.result_value()
assert result is not None
step, kind = result
assert step["template"].startswith("cond_from_failure_")
assert (d / step["template"]).exists()
assert (d / step["mask"]).exists()
print("3) ConditionPickerDialog.on_pick_from_failure: OK ->", step["template"], kind)

print("\n=== from-failure features: ALL CHECKS PASSED ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)
