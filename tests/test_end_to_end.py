import sys, pathlib, tempfile, shutil, hashlib
import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_e2e_"))

import core
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets, QtCore
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

NAME = "e2e"
d = core.recipe_dir(NAME)


def make_tex(label):
    return np.random.RandomState(abs(hash(label)) % (2**31)).randint(0, 256, (16, 16), dtype=np.uint8)


for label in ["A", "B", "C", "D", "E", "COND"]:
    Image.fromarray(make_tex(label)).save(d / f"{label}.png")

# 記録直後を模した、まだ分岐の無いフラットなレシピ
steps = [
    {"type": "tap", "label": "A", "template": "A.png", "context": "A.png",
     "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
    {"type": "tap", "label": "B", "template": "B.png", "context": "B.png",
     "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
    {"type": "tap", "label": "C", "template": "C.png", "context": "C.png",
     "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
    {"type": "tap", "label": "E", "template": "E.png", "context": "E.png",
     "x": 1, "y": 1, "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
]
core.save_recipe(NAME, {"device_size": [200, 200], "screenshot_size": [200, 200],
                         "popups": [], "steps": steps})

# --- 編集画面でB,Cをifのthenにまとめて保存 ---
dlg = gui.StructureEditorDialog(NAME)
b_item = dlg.tree.topLevelItem(1)
c_item = dlg.tree.topLevelItem(2)
b_item.setSelected(True)
c_item.setSelected(True)
gui.ConditionPickerDialog.pick = staticmethod(lambda parent, recipe_dir, steps: (
    next(s for s in steps if s["label"] == "A"), "image_found"))
QtWidgets.QInputDialog.getText = staticmethod(lambda *a, **kw: ("分岐AB", True))
dlg.on_make_if()
# ピッカーは既存ステップ(A)の画像を条件として選ぶ実際の挙動を確認済み
# (test_structure_editor.py)。ここではA自身がタップで消えてしまい判定が
# 汚染されるのを避けるため、テスト用に条件の参照先だけ専用画像に差し替える
# (recipe.json上の形はピッカーが作るものと同一で、参照先ファイルが違うだけ)
if_node_for_test = next(s for s in dlg.data["steps"] if s.get("type") == "if")
if_node_for_test["condition"]["template"] = "COND.png"
dlg.save()
print("編集画面で保存した構造:", [s.get("label", s.get("type")) for s in dlg.data["steps"]])

# --- core.load_recipe で読み込めるか(画像も含めて) ---
loaded = core.load_recipe(NAME)
assert loaded["steps"][0]["label"] == "A"
assert loaded["steps"][1]["type"] == "if"
assert loaded["steps"][1]["condition"]["_gray"] is not None
assert loaded["steps"][1]["then"][0]["label"] == "B"
assert loaded["steps"][1]["then"][0]["_gray"] is not None
print("core.load_recipe: OK (条件・タップ双方の画像が読み込めた)")

# --- PlayerThread で実際に再生してthenを通ってEまで到達するか ---
LABEL_BY_HASH = {}
for label in ["A", "B", "C", "D", "E", "COND"]:
    arr = make_tex(label)
    LABEL_BY_HASH[hashlib.sha1(arr.tobytes()).hexdigest()] = label


class FakeDevice:
    def __init__(self):
        self.serial = "fake"

    def screenshot(self):
        return Image.new("RGB", (200, 200))

    def window_size(self):
        return (200, 200)

    def press(self, *a, **kw):
        pass


call_count = {}


def fake_peak_match(screen_gray, tpl_gray, method="ccoeff", mask=None):
    h = hashlib.sha1(tpl_gray.tobytes()).hexdigest()
    label = LABEL_BY_HASH.get(h, "?")
    n = call_count.get(label, 0)
    call_count[label] = n + 1
    if label in ("A", "B", "C", "E"):
        found = n == 0
    elif label == "COND":
        found = True  # 条件用の専用画像は消費されず常に判定できる
    else:
        found = False
    return (50, 50, 0.95) if found else (50, 50, 0.1)


core.peak_match = fake_peak_match
core.tap = lambda *a, **kw: None
core.connect = lambda serial=None: FakeDevice()

logs = []
t = gui.PlayerThread(None, NAME, 1, 0.0, 3, 0.02, 0.02, 0, 3, verify=True, tap_retry=1, hold_ms=0)
t.sig_log.connect(lambda m: logs.append(m))
t.run()
for l in logs:
    print(l)

assert any("then へ" in l for l in logs)
assert any("成功1" in l for l in logs)
print("\n=== END-TO-END (editor -> load_recipe -> PlayerThread): PASSED ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)
