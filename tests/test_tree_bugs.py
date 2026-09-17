import sys, pathlib, tempfile, shutil, json
import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_treebugs_"))

import core
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets, QtCore, QtGui
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

QtWidgets.QMessageBox.information = staticmethod(lambda *a, **kw: None)
QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **kw: print("  [warning]", a[1:3]))

NAME = "treebugs_test"
d = core.recipe_dir(NAME)

FULL_W, FULL_H = 200, 300


def make_textured(seed, w=16, h=16):
    return np.random.RandomState(seed).randint(0, 256, (h, w), dtype=np.uint8)


# A(タップ), IF(条件=B, then=[B], else=[]), C, D
# skip-search対象: Aの直後にIFが来る構造。Aを待っている最中に、siblings_ahead
# で "B" や "C" が候補に混ざるが、間にIFノードがあるので "IF" 自体は候補
# には出てこない(if-nodeには_grayが無いのでcandidatesに直接生では並ばない)。
# ここでは cursor.siblings_ahead() の生の戻り値そのものにifノードが含まれる
# ケースをテストする: A の直後が IF なので、Aを待っている間の候補は [IF] になる
imgs = {}
for label in ["A", "B", "C", "D"]:
    arr = make_textured(hash(label) % 1000)
    Image.fromarray(arr).save(d / f"{label}.png")
    imgs[label] = arr

steps = [
    {"type": "tap", "label": "A", "template": "A.png", "context": "A.png",
     "x": 8, "y": 8, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
    {"type": "if", "label": "分岐",
     "condition": {"kind": "image_found", "template": "B.png", "method": "ccoeff", "threshold": 0.8},
     "then": [
         {"type": "tap", "label": "B", "template": "B.png", "context": "B.png",
          "x": 8, "y": 8, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
     ], "else": []},
    {"type": "tap", "label": "C", "template": "C.png", "context": "C.png",
     "x": 8, "y": 8, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
    {"type": "tap", "label": "D", "template": "D.png", "context": "D.png",
     "x": 8, "y": 8, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
]
core.save_recipe(NAME, {"device_size": [FULL_W, FULL_H], "screenshot_size": [FULL_W, FULL_H],
                         "popups": [], "steps": steps})

# ============================================================
# 【1】スキップ探索がifノードでクラッシュしない
# ============================================================
reloaded = core.load_recipe(NAME)
cursor = core.Cursor(reloaded["steps"])
cur = cursor.current()
assert cur["label"] == "A"
cursor.advance()  # Aを実行済みにして、IFノードの手前にいる状態を再現
cur2 = cursor.current()
assert cur2["type"] == "if", cur2

# Aを待っている状況を模して、Aの位置までカーソルを戻さずそのまま
# siblings_ahead()を直接呼ぶ(Aの次の兄弟=IFノードが候補に入るケース)
cursor2 = core.Cursor(reloaded["steps"])
candidates = cursor2.siblings_ahead(5)
assert candidates[0]["type"] == "if", candidates[0]
print("1a) siblings_ahead()の生の戻り値にifノードが含まれる(想定通り): OK")

# PlayerThreadの_find_best_matchが、このcandidatesを渡されてもKeyErrorせず、
# ifより先(C, D)を誤ってスキップ探索の対象にもしないことを確認する
pt = gui.PlayerThread.__new__(gui.PlayerThread)


def fake_match_candidate(gray, s):
    # Cにだけ非常に高い一致を返す(もし_find_best_matchがifを飛び越えて
    # Cまで見てしまうバグが残っていれば、これがヒットしてしまう)
    if s.get("label") == "C":
        return 5, 5, 0.99, "ccoeff", 0.8, []
    return None, None, None, None, None, None


pt._match_candidate = fake_match_candidate
dummy_gray = np.zeros((FULL_H, FULL_W), dtype=np.uint8)
result = pt._find_best_match(dummy_gray, candidates)
assert result is None, (
    "ifノードで打ち切らず、その先のCまでスキップ探索してしまっている: " + repr(result))
print("1b) _find_best_match: candidatesの先頭がifノード -> KeyErrorせず、"
      "即座に探索を打ち切る(Cへは絶対に飛ばない): OK")

# 比較: ifが無い(全部tap)候補ならCが見つかることも確認しておく(誤って
# 常にNoneを返すよう壊していないことの確認)
tap_only_candidates = [n for n in candidates if n.get("type", "tap") == "tap"] + [
    {"type": "tap", "label": "C", "template": "C.png", "_gray": imgs["C"],
     "x": 8, "y": 8, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
]
result2 = pt._find_best_match(dummy_gray, tap_only_candidates)
assert result2 is not None and result2[1]["label"] == "C"
print("1c) if が混ざっていないtapだけの候補なら通常通り検出できる: OK")

# ============================================================
# 【2】記録内容タブがthen/elseの中身をツリー表示する
# ============================================================
list_steps = gui.BlockTreeWidget()
gui.NodeTreeBuilder(list_steps, d, editable=False).build(reloaded_data := json.loads(
    (d / "recipe.json").read_text(encoding="utf-8"))["steps"])

top_labels = [list_steps.topLevelItem(i).text(0) for i in range(list_steps.topLevelItemCount())]
assert top_labels == ["A", "[if] 分岐", "C", "D"], top_labels
print("2a) トップレベルにA, [if]分岐, C, D が並ぶ: OK ->", top_labels)

if_item = list_steps.topLevelItem(1)
then_header = if_item.child(0)
assert then_header.text(0) == "then:"
b_item = then_header.child(0)
assert b_item.text(0) == "B"
role_b = list_steps.get_role(b_item)
assert role_b["path"] == (1, "then", 0), role_b["path"]
print("2b) then:の中に「B」がインデント付きで表示され、pathも正しい"
      "((1,'then',0)): OK")

else_header = if_item.child(1)
assert else_header.text(0) == "else:"
assert else_header.child(0).text(0) == "(空)"
print("2c) elseは空なので (空) が表示される: OK")

role_a = list_steps.get_role(list_steps.topLevelItem(0))
assert role_a["path"] == (0,)
role_c = list_steps.get_role(list_steps.topLevelItem(2))
assert role_c["path"] == (2,)
print("2d) A/Cのpathがそれぞれ(0,), (2,): OK")

# ============================================================
# 【3】マスク調整が行番号ではなくpathでノードを特定する(誤って別ノードを
#      書き換えない)
# ============================================================
# 「分岐より後ろのステップ」であるC(path=(2,))のマスクを調整したとき、
# 意図通りCだけが更新され、D(path=(3,))やAは一切変更されないことを確認する
before = json.loads((d / "recipe.json").read_text(encoding="utf-8"))
before_D = json.loads(json.dumps(before["steps"][3]))  # ディープコピー
before_A = json.loads(json.dumps(before["steps"][0]))

dlg = gui.MaskEditorDialog(NAME, "steps", (2,))  # C
assert dlg.node["label"] == "C", dlg.node["label"]
dlg.on_click_tap_pos(int(9 * dlg.preview_scale), int(9 * dlg.preview_scale))
dlg.sp_threshold.setValue(0.77)
dlg.save()

after = json.loads((d / "recipe.json").read_text(encoding="utf-8"))
assert after["steps"][2]["label"] == "C"
assert after["steps"][2]["threshold"] == 0.77, after["steps"][2]["threshold"]
assert after["steps"][3] == before_D, "Dが意図せず書き換わっている"
assert after["steps"][0] == before_A, "Aが意図せず書き換わっている"
# then内のBも無事(誤って触っていない)
assert after["steps"][1]["then"][0]["label"] == "B"
assert after["steps"][1]["then"][0]["threshold"] == 0.8
print("3) 分岐より後ろのC(path=(2,))を調整 -> Cだけが更新され、"
      "A/B/Dは無傷: OK")

# 更に、then内のB(path=(1,'then',0))を直接指定して調整できることも確認
dlg2 = gui.MaskEditorDialog(NAME, "steps", (1, "then", 0))
assert dlg2.node["label"] == "B", dlg2.node["label"]
dlg2.sp_threshold.setValue(0.66)
dlg2.save()
after2 = json.loads((d / "recipe.json").read_text(encoding="utf-8"))
assert after2["steps"][1]["then"][0]["threshold"] == 0.66
print("3b) then内のB(path=(1,'then',0))を直接指定して調整できる: OK")

print("\n=== if/elseツリー化に伴う3件の不具合修正: ALL CHECKS PASSED ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)
