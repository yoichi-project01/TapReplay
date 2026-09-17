import sys, pathlib, tempfile, shutil, hashlib
import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_ifengine_"))

import core
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui


def make_template(label, size=16):
    """is_distinctive()(std>=35)を満たす、ラベルごとに再現可能な
    テクスチャ付きテンプレートを作る(定数値の画像はstd=0で弾かれるため)"""
    rng = np.random.RandomState(abs(hash(label)) % (2**31))
    arr = rng.randint(0, 256, size=(size, size), dtype=np.uint8)
    return arr


LABEL_BY_HASH = {}


def save_template(d, fname, label):
    arr = make_template(label)
    Image.fromarray(arr).save(d / fname)
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


def install_mocks(found_fn):
    """found_fn(label, occurrence_index) -> bool を元に peak_match をモックする"""
    call_count = {}

    def fake_peak_match(screen_gray, tpl_gray, method="ccoeff", mask=None):
        h = hashlib.sha1(tpl_gray.tobytes()).hexdigest()
        label = LABEL_BY_HASH.get(h, "?")
        n = call_count.get(label, 0)
        call_count[label] = n + 1
        found = found_fn(label, n)
        return (50, 50, 0.95) if found else (50, 50, 0.1)

    core.peak_match = fake_peak_match
    core.tap = lambda *a, **kw: None
    core.connect = lambda serial=None: FakeDevice()
    return call_count


def tap_node(label, template):
    return {"type": "tap", "label": label, "template": template, "x": 1, "y": 1,
            "dx": 0, "dy": 0, "method": "ccoeff", "threshold": 0.8}


def build_if_recipe(name):
    d = core.recipe_dir(name)
    for label, fname in [("A", "A.png"), ("B", "B.png"), ("C", "C.png"),
                          ("D", "D.png"), ("E", "E.png"), ("COND", "cond.png")]:
        save_template(d, fname, label)
    steps = [
        tap_node("A", "A.png"),
        {"type": "if", "label": "分岐X",
         "condition": {"kind": "image_found", "template": "cond.png",
                       "method": "ccoeff", "threshold": 0.8},
         "then": [tap_node("B", "B.png"), tap_node("C", "C.png")],
         "else": [tap_node("D", "D.png")]},
        tap_node("E", "E.png"),
    ]
    core.save_recipe(name, {"device_size": [200, 200], "screenshot_size": [200, 200],
                             "popups": [], "steps": steps})


def run(name, loops, step_timeout, found_fn):
    logs = []
    call_count = install_mocks(found_fn)
    t = gui.PlayerThread(None, name, loops, 0.0, step_timeout, 0.02, 0.02, 0, 3,
                          verify=True, tap_retry=1, hold_ms=0)
    t.sig_log.connect(lambda m: logs.append(m))
    t.run()
    return logs


print("###### 1) 条件成立: then(B,C)を通ってEへ ######")


def found_then(label, n):
    if label == "COND":
        return True
    if label in ("A", "B", "C", "E"):
        return n == 0  # 初回だけ見え、タップ後は消える
    return False  # D(else)は一切見えない


build_if_recipe("case_then")
logs = run("case_then", 1, 3, found_then)
for l in logs:
    print(l)
assert any("then へ" in l for l in logs)
assert not any("D: タップ" in l for l in logs), "elseのDが実行されてしまった"
assert any("成功1" in l for l in logs), "成功1で終わるはず"
print("OK: then分岐が選ばれ、E まで到達し成功1で終了\n")

print("###### 2) 条件不成立: else(D)を通ってEへ ######")


def found_else(label, n):
    if label == "COND":
        return False
    if label in ("A", "D", "E"):
        return n == 0
    return False  # B,C(then)は一切見えない


build_if_recipe("case_else")
logs = run("case_else", 1, 3, found_else)
for l in logs:
    print(l)
assert any("else へ" in l for l in logs)
assert not any("B: タップ" in l or "C: タップ" in l for l in logs), "thenのB/Cが実行されてしまった"
assert any("成功1" in l for l in logs), "成功1で終わるはず"
print("OK: else分岐が選ばれ、E まで到達し成功1で終了\n")

print("###### 3) 封じ込めテスト: then内でBを待つ間、D/Eが常に見えていてもスキップしない ######")


def found_containment(label, n):
    if label == "COND":
        return True  # thenへ
    if label == "A":
        return n == 0
    if label == "B":
        return False  # Bは絶対見つからない(タイムアウトさせる)
    if label in ("D", "E"):
        return True  # else・親ブロックの中身が"常に見えている"状況を偽装
    return False


build_if_recipe("case_containment")
logs = run("case_containment", 1, 1, found_containment)  # 1秒で早めにタイムアウト
for l in logs:
    print(l)
assert not any("スキップ" in l for l in logs), "then内からD/Eへスキップしてしまった"
assert any("B が出現せず" in l for l in logs), "Bのタイムアウト失敗が記録されていない"
print("OK: D/Eが常に一致していてもスキップ探索は発動しなかった(封じ込め確認)\n")

print("=== ALL if-engine simulated checks PASSED ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)

# PySide6+numpy/OpenCVの組み合わせで、テスト自体は全て成功しているにも
# 関わらず、インタプリタ終了時のガベージコレクション中にネイティブ側で
# access violationが起きることがある(実測で確認済み、Pythonフレームを
# 持たないクラッシュのため原因の特定はできていない)。判定はここまでの
# assertで完全に終わっているため、後始末を経由しない即時終了で回避する
# (os._exitはatexit/バッファのflushをしないため、明示的にflushしてから使う)
import os
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)
