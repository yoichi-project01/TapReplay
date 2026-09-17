import sys, pathlib, tempfile, shutil, json
import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_delete_"))

import core
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets, QtCore
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

QtWidgets.QMessageBox.question = staticmethod(lambda *a, **kw: QtWidgets.QMessageBox.Yes)

NAME = "delete_test"
d = core.recipe_dir(NAME)
for label in ["1", "2", "3", "4", "5"]:
    arr = np.random.RandomState(int(label)).randint(0, 256, (8, 8), dtype=np.uint8)
    Image.fromarray(arr).save(d / f"s{label}.png")

# 1, IF(then=[2,3], else=[]), 4, 5
steps = [
    {"type": "tap", "label": "S1", "template": "s1.png", "context": "s1.png",
     "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
    {"type": "if", "label": "分岐",
     "condition": {"kind": "image_found", "template": "s2.png", "method": "ccoeff", "threshold": 0.8},
     "then": [
         {"type": "tap", "label": "S2", "template": "s2.png", "context": "s2.png",
          "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
         {"type": "tap", "label": "S3", "template": "s3.png", "context": "s3.png",
          "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
     ], "else": []},
    {"type": "tap", "label": "S4", "template": "s4.png", "context": "s4.png",
     "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
    {"type": "tap", "label": "S5", "template": "s5.png", "context": "s5.png",
     "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
]
popups = [{"label": "P1", "template": "s1.png", "context": "s1.png", "x": 1, "y": 1,
           "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8}]
core.save_recipe(NAME, {"device_size": [200, 200], "screenshot_size": [200, 200],
                         "popups": popups, "steps": steps})

# --- 1) StructureEditorDialog: 単一ステップの削除(S4) ---
dlg = gui.StructureEditorDialog(NAME)
s4_item = dlg.tree.topLevelItem(2)
assert dlg.tree.get_role(s4_item)["node"]["label"] == "S4"
s4_item.setSelected(True)
dlg.on_delete_selected()
labels = [s.get("label", s.get("type")) for s in dlg.data["steps"]]
assert labels == ["S1", "分岐", "S5"], labels
print("1) 単一ステップ削除(S4): OK ->", labels)

# --- 2) ifブロックごと削除(中身のS2,S3も一緒に消える) ---
dlg._rebuild_tree()
if_item = dlg.tree.topLevelItem(1)
assert dlg.tree.get_role(if_item)["node"]["type"] == "if"
if_item.setSelected(True)
dlg.on_delete_selected()
labels2 = [s.get("label", s.get("type")) for s in dlg.data["steps"]]
assert labels2 == ["S1", "S5"], labels2
print("2) ifブロックごと削除(中身含む): OK ->", labels2)

dlg.save()
reloaded = core.load_recipe(NAME)
assert [s["label"] for s in reloaded["steps"]] == ["S1", "S5"]
print("   保存 -> core.load_recipeで正しく反映: OK")

# --- 3) 複数選択での同時削除(同一階層の2件) ---
NAME2 = "delete_test2"
d2 = core.recipe_dir(NAME2)
for label in ["a", "b", "c"]:
    arr = np.random.RandomState(hash(label) % 1000).randint(0, 256, (8, 8), dtype=np.uint8)
    Image.fromarray(arr).save(d2 / f"{label}.png")
steps2 = [
    {"type": "tap", "label": "A", "template": "a.png", "context": "a.png",
     "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
    {"type": "tap", "label": "B", "template": "b.png", "context": "b.png",
     "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
    {"type": "tap", "label": "C", "template": "c.png", "context": "c.png",
     "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
]
core.save_recipe(NAME2, {"device_size": [200, 200], "screenshot_size": [200, 200],
                          "popups": [], "steps": steps2})
dlg2 = gui.StructureEditorDialog(NAME2)
dlg2.tree.topLevelItem(0).setSelected(True)  # A
dlg2.tree.topLevelItem(2).setSelected(True)  # C
dlg2.on_delete_selected()
labels3 = [s.get("label") for s in dlg2.data["steps"]]
assert labels3 == ["B"], labels3
print("3) 複数選択(A,Cを同時削除): OK ->", labels3)

# --- 4) 記録内容タブ: 共通ポップアップの削除 ---
class FakeMainForPopupDelete:
    pass


mw_cmb = QtWidgets.QComboBox()
mw_cmb.addItem(NAME)
mw_list_popups = QtWidgets.QListWidget()
mw_list_popups.addItem("P1")
mw_list_popups.setCurrentRow(0)


class Ctx:
    cmb_recipe = mw_cmb
    list_popups = mw_list_popups

    def append(self, msg):
        print("  [log]", msg)

    def refresh_history(self):
        pass


ctx = Ctx()
gui.MainWindow.on_delete_popup(ctx)
after = json.loads((d / "recipe.json").read_text(encoding="utf-8"))
assert after.get("popups", []) == [], after.get("popups")
print("4) 共通ポップアップ削除: OK -> popups =", after.get("popups"))

print("\n=== delete features: ALL CHECKS PASSED ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)
