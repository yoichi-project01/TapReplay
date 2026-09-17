import sys, pathlib, tempfile, shutil, json
import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_unblock_identity_"))

import core
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **kw: print("  [warning]", a[1:3]))

NAME = "unblock_identity_test"
d = core.recipe_dir(NAME)
for label in ["x", "y"]:
    arr = np.random.RandomState(hash(label) % 1000).randint(0, 256, (8, 8), dtype=np.uint8)
    Image.fromarray(arr).save(d / f"{label}.png")

# 内容が完全に等しい(条件・label・then中身まで同一)ifブロックを2つ並べる。
# list.index()のような==一致だと、後ろのif(IF2)を解除したつもりでも
# 先頭のIF1が誤って解除されてしまう
cond = {"kind": "image_found", "template": "x.png", "method": "ccoeff", "threshold": 0.8}
tap_x = {"type": "tap", "label": "X", "template": "x.png", "context": "x.png",
         "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8}
if1 = {"type": "if", "label": "同名分岐", "condition": dict(cond), "then": [dict(tap_x)], "else": []}
if2 = {"type": "if", "label": "同名分岐", "condition": dict(cond), "then": [dict(tap_x)], "else": []}
tap_y = {"type": "tap", "label": "Y", "template": "y.png", "context": "y.png",
         "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8}
steps = [if1, tap_y, if2]
core.save_recipe(NAME, {"device_size": [200, 200], "screenshot_size": [200, 200],
                         "popups": [], "steps": steps})

dlg = gui.StructureEditorDialog(NAME)
# 内容が同一の2つのifが並んでいることを確認(前提条件)
assert dlg.data["steps"][0] == dlg.data["steps"][2], "テストの前提が崩れている"

# 後ろ(3番目=index 2)のIF2を選択して解除する
if2_item = dlg.tree.topLevelItem(2)
assert dlg.tree.get_role(if2_item)["node"] is dlg.data["steps"][2]
if2_item.setSelected(True)
dlg.on_unblock()

labels = [s.get("label") for s in dlg.data["steps"]]
# IF2(後ろ)が解除されて中身のXが展開され、IF1(前)はそのまま残っているはず
assert labels == ["同名分岐", "Y", "X"], labels
assert dlg.data["steps"][0]["type"] == "if", "先頭のIF1が誤って解除された(list.index()のバグ)"
print("選択した後ろのIF2だけが正しく解除され、内容が同一の先頭IF1は"
      "無事に残っている: OK ->", labels)

print("\n=== on_unblock identity-safe fix: ALL CHECKS PASSED ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)
