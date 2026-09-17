import sys, pathlib, tempfile, shutil, json
import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_editor_"))

import core
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets, QtCore
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

NAME = "editor_test"
d = core.recipe_dir(NAME)
for label in ["1", "2", "3", "4", "5"]:
    arr = np.random.RandomState(int(label)).randint(0, 256, (8, 8), dtype=np.uint8)
    Image.fromarray(arr).save(d / f"step_{label}.png")
steps = [
    {"type": "tap", "label": f"ステップ{i}", "template": f"step_{i}.png",
     "context": f"step_{i}.png", "x": 1, "y": 1, "dx": 0, "dy": 0,
     "method": "ccoeff", "threshold": 0.8}
    for i in ["1", "2", "3", "4", "5"]
]
core.save_recipe(NAME, {"device_size": [200, 200], "screenshot_size": [200, 200],
                         "popups": [], "steps": steps})

# --- 1) 開いて、ステップ2,3を選択して「then にする」 ---
dlg = gui.StructureEditorDialog(NAME)
assert dlg.tree.topLevelItemCount() == 5

item2 = dlg.tree.topLevelItem(1)  # ステップ2
item3 = dlg.tree.topLevelItem(2)  # ステップ3
item2.setSelected(True)
item3.setSelected(True)

# ConditionPickerDialog.pick / QInputDialog.getText はUI操作が要るので固定応答にする
orig_pick = gui.ConditionPickerDialog.pick
orig_get_text = QtWidgets.QInputDialog.getText
gui.ConditionPickerDialog.pick = staticmethod(
    lambda parent, recipe_dir, steps: (steps[0], "image_found"))
QtWidgets.QInputDialog.getText = staticmethod(lambda *a, **kw: ("分岐A", True))

dlg.on_make_if()

QtWidgets.QInputDialog.getText = orig_get_text
gui.ConditionPickerDialog.pick = orig_pick

assert len(dlg.data["steps"]) == 4, [s.get("label") for s in dlg.data["steps"]]
if_node = dlg.data["steps"][1]
assert if_node["type"] == "if"
assert if_node["label"] == "分岐A"
assert [s["label"] for s in if_node["then"]] == ["ステップ2", "ステップ3"]
assert if_node["else"] == []
assert dlg.tree.topLevelItemCount() == 4
print("1) make_if: OK ->", [s.get("label", s.get("type")) for s in dlg.data["steps"]])

# --- 2) 保存して、新しいダイアログで再読み込み(往復できるか) ---
dlg.save()
dlg2 = gui.StructureEditorDialog(NAME)
assert len(dlg2.data["steps"]) == 4
if_node2 = dlg2.data["steps"][1]
assert if_node2["type"] == "if"
assert [s["label"] for s in if_node2["then"]] == ["ステップ2", "ステップ3"]
print("2) 保存→再読み込み: OK")

# --- 3) ドラッグ&ドロップ相当: ツリー上でステップ1とifブロックの順番を
#         QTreeWidget操作で入れ替え、_on_rows_movedでデータに反映されるか ---
top0 = dlg2.tree.takeTopLevelItem(0)  # ステップ1を抜く
dlg2.tree.insertTopLevelItem(1, top0)  # ifブロックの後ろに挿す
dlg2._on_rows_moved()
labels = [s.get("label") for s in dlg2.data["steps"]]
assert labels[0] == "分岐A", labels
assert labels[1] == "ステップ1", labels
print("3) 並び替え反映(_on_rows_moved): OK ->", labels)

# --- 4) ブロックの解除 ---
dlg2._rebuild_tree()
if_item = None
for i in range(dlg2.tree.topLevelItemCount()):
    it = dlg2.tree.topLevelItem(i)
    role = dlg2.tree.get_role(it)
    if role["node"].get("type") == "if":
        if_item = it
        break
assert if_item is not None
if_item.setSelected(True)
dlg2.on_unblock()
labels_after = [s.get("label") for s in dlg2.data["steps"]]
assert "分岐A" not in [s.get("label") for s in dlg2.data["steps"] if s.get("type") == "if"], labels_after
assert "ステップ2" in labels_after and "ステップ3" in labels_after
print("4) ブロック解除: OK ->", labels_after)

# --- 5) 深さ制限: 3階層目までのifを作り、4階層目を拒否できるか ---
NAME2 = "depth_test"
d2 = core.recipe_dir(NAME2)
for i in range(1, 4):
    arr = np.random.RandomState(100 + i).randint(0, 256, (8, 8), dtype=np.uint8)
    Image.fromarray(arr).save(d2 / f"s{i}.png")
steps2 = [
    {"type": "tap", "label": f"S{i}", "template": f"s{i}.png", "context": f"s{i}.png",
     "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8}
    for i in range(1, 4)
]
core.save_recipe(NAME2, {"device_size": [200, 200], "screenshot_size": [200, 200],
                          "popups": [], "steps": steps2})

dlg3 = gui.StructureEditorDialog(NAME2)
gui.ConditionPickerDialog.pick = staticmethod(
    lambda parent, recipe_dir, steps: (steps[0], "image_found"))
QtWidgets.QInputDialog.getText = staticmethod(lambda *a, **kw: ("L1", True))

# depth1 -> if作成(中身はdepth2になる)
dlg3.tree.topLevelItem(0).setSelected(True)
dlg3.on_make_if()  # S1をthenに包んだ if(L1) ができる。thenはdepth2

# thenの中のS1を選んでさらにifにする(depth2 -> 中身depth3)
if_item = dlg3.tree.topLevelItem(0)
then_header = if_item.child(0)
inner_step = then_header.child(0)
inner_step.setSelected(True)
QtWidgets.QInputDialog.getText = staticmethod(lambda *a, **kw: ("L2", True))
dlg3.on_make_if()  # OKのはず(depth2 -> 中身depth3)

# さらにその中(depth3)のノードを選んでifにしようとする -> depth3+1=4 は拒否されるはず
if_item = dlg3.tree.topLevelItem(0)
then_header = if_item.child(0)
inner_if_item = then_header.child(0)
inner_then_header = inner_if_item.child(0)
deepest_step = inner_then_header.child(0)
deepest_step.setSelected(True)

warned = {"called": False}
orig_warning = QtWidgets.QMessageBox.warning


def fake_warning(*a, **kw):
    warned["called"] = True
    return QtWidgets.QMessageBox.Ok


QtWidgets.QMessageBox.warning = staticmethod(fake_warning)
before = json.dumps(dlg3.data)
dlg3.on_make_if()
after = json.dumps(dlg3.data)
QtWidgets.QMessageBox.warning = orig_warning
gui.ConditionPickerDialog.pick = orig_pick
QtWidgets.QInputDialog.getText = orig_get_text

assert warned["called"], "4階層目を作ろうとしたのに警告が出なかった"
assert before == after, "拒否されたはずなのにデータが変わっている"
print("5) 深さ制限(3階層まで): OK (4階層目は拒否された)")

print("\n=== StructureEditorDialog: ALL CHECKS PASSED ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)
