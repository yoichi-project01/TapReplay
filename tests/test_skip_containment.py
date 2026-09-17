import sys, pathlib, tempfile, shutil
import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_skip_"))

import core
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

NAME = "skip_containment"
d = core.recipe_dir(NAME)
for i, fname in enumerate(["step_A.png", "step_B.png", "step_C.png",
                            "step_D.png", "step_E.png", "cond.png"]):
    arr = np.full((8, 8), 10 + i * 20, dtype=np.uint8)
    Image.fromarray(arr).save(d / fname)

# A -> IF(then=[B, C], else=[D]) -> E
steps = [
    {"type": "tap", "label": "A", "template": "step_A.png", "x": 1, "y": 1,
     "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
    {"type": "if", "label": "分岐X",
     "condition": {"kind": "image_found", "template": "cond.png",
                   "method": "ccoeff", "threshold": 0.8},
     "then": [
         {"type": "tap", "label": "B", "template": "step_B.png", "x": 1, "y": 1,
          "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
         {"type": "tap", "label": "C", "template": "step_C.png", "x": 1, "y": 1,
          "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
     ],
     "else": [
         {"type": "tap", "label": "D", "template": "step_D.png", "x": 1, "y": 1,
          "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
     ]},
    {"type": "tap", "label": "E", "template": "step_E.png", "x": 1, "y": 1,
     "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
]
core.save_recipe(NAME, {"device_size": [200, 200], "screenshot_size": [200, 200],
                         "popups": [], "steps": steps})


class FakeDevice:
    def __init__(self):
        self.serial = "fake"

    def screenshot(self):
        return Image.new("RGB", (200, 200))

    def window_size(self):
        return (200, 200)

    def press(self, *a, **kw):
        pass


# シナリオ: 条件成立でthenへ入り、Bを待っている間、
# ずっと"D"(elseにある)と"E"(ifの後、親ブロックにある)が高い一致度で
# 「見えている」状態を作る(現実にはあり得ない設定だが、意図的に
# スキップ探索がそれらに飛びつかないことを確認するため)。
# Bは一切見つからない(タイムアウトするはず)。
call_log = []


def fake_peak_match(screen_gray, tpl_gray, method="ccoeff", mask=None):
    val = float(tpl_gray.mean())
    idx = round((val - 10) / 20)
    labels = ["A", "B", "C", "D", "E", "COND"]
    label = labels[idx] if 0 <= idx < len(labels) else "?"
    call_log.append(label)
    if label == "A":
        # 最初の1回だけ見つかって消える
        found = call_log.count("A") <= 1
    elif label == "COND":
        found = True  # thenへ入る
    elif label == "B":
        found = False  # Bは絶対に見つからない(タイムアウトさせる)
    elif label in ("D", "E"):
        found = True  # elseの中身・ifの後の兄弟が「見えている」ように見せかける
    else:
        found = False
    return (50, 50, 0.95) if found else (50, 50, 0.1)


orig_peak_match = core.peak_match
orig_tap = core.tap
orig_connect = core.connect
core.peak_match = fake_peak_match
core.tap = lambda *a, **kw: None
core.connect = lambda serial=None: FakeDevice()

logs = []
t = gui.PlayerThread(
    None, NAME, 1, 0.0, 2, 0.02, 0.02, 0, 3,  # step_timeout=2秒で早めにタイムアウトさせる
    verify=True, tap_retry=1, hold_ms=0,
)
t.sig_log.connect(lambda m: logs.append(m))

try:
    t.run()
finally:
    core.peak_match = orig_peak_match
    core.tap = orig_tap
    core.connect = orig_connect

for l in logs:
    print(l)

print()
skip_lines = [l for l in logs if "スキップ" in l]
print("スキップ発生行数:", len(skip_lines))
assert len(skip_lines) == 0, f"then内からD/Eへスキップしてしまった: {skip_lines}"
timeout_lines = [l for l in logs if "失敗" in l and "B" in l]
print("Bのタイムアウト検出:", bool(timeout_lines))
print("=== containment across then -> else/parent: CONFIRMED NO SKIP ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)
