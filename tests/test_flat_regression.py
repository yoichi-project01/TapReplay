import sys, pathlib, tempfile, shutil, hashlib
import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_flat_"))

import core
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

NAME = "flat_regression"
d = core.recipe_dir(NAME)
# is_distinctive(min_std=35)のフィルタに引っかからないよう、定数値ではなく
# 実際に画素ごとのばらつきがあるテンプレートを使う(でないとスキップ探索の
# 候補から常に除外されてしまい、このテスト自体が意味を成さなくなる)
hash_to_label = {}
for i, fname in enumerate(["step_1.png", "step_2.png", "step_3.png"]):
    arr = np.random.RandomState(100 + i).randint(0, 256, (8, 8), dtype=np.uint8)
    Image.fromarray(arr).save(d / fname)
    hash_to_label[hashlib.sha1(arr.tobytes()).hexdigest()] = f"ステップ{i + 1}"

# 旧形式そのまま: type キーなし(setdefaultでtapになるはず)
steps = [
    {"label": "ステップ1", "template": "step_1.png", "x": 1, "y": 1,
     "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
    {"label": "ステップ2", "template": "step_2.png", "x": 1, "y": 1,
     "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8},
    {"label": "ステップ3", "template": "step_3.png", "x": 1, "y": 1,
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


call_count = {}


def fake_peak_match(screen_gray, tpl_gray, method="ccoeff", mask=None):
    label = hash_to_label[hashlib.sha1(tpl_gray.tobytes()).hexdigest()]
    n = call_count.get(label, 0)
    call_count[label] = n + 1
    # ステップ2は最初見つからず、ステップ3のスキップ探索で拾われる想定にする
    if label == "ステップ2":
        found = False
    else:
        found = (n == 0)
    return (50, 50, 0.95) if found else (50, 50, 0.1)


orig_peak_match = core.peak_match
orig_tap = core.tap
orig_connect = core.connect
core.peak_match = fake_peak_match
core.tap = lambda *a, **kw: None
core.connect = lambda serial=None: FakeDevice()

logs = []
t = gui.PlayerThread(
    None, NAME, 1, 0.0, 1, 0.02, 0.02, 0, 3,
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

assert any("スキップして" in l and "ステップ3" in l for l in logs), "スキップ探索が発動しなかった"
assert any("不完全" in l for l in logs), "スキップ周は不完全扱いになるはず"
print()
print("=== flat recipe regression: OK (skip-search still works, label-only log) ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)
