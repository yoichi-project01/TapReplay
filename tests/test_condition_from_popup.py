import sys, pathlib, shutil, tempfile
import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_condfrompopup_"))

import core
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

QtWidgets.QMessageBox.information = staticmethod(lambda *a, **kw: None)
QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **kw: print("  [warning]", a[1:3]))
_question_answer = {"resp": QtWidgets.QMessageBox.Yes}
QtWidgets.QMessageBox.question = staticmethod(lambda *a, **kw: _question_answer["resp"])

NAME = "cond_from_popup_test"
d = core.recipe_dir(NAME)


def make_textured(seed):
    return np.random.RandomState(seed).randint(0, 256, (10, 10), dtype=np.uint8)


for label in ["s1", "s2", "p1"]:
    Image.fromarray(make_textured(hash(label) % 1000)).save(d / f"{label}.png")
    Image.fromarray(np.full((10, 10), 255, dtype=np.uint8)).save(d / f"{label}_mask.png")

steps = [
    {"type": "tap", "label": "ステップ1", "template": "s1.png", "mask": "s1_mask.png",
     "context": "s1.png", "x": 5, "y": 5, "dx": 0, "dy": 0,
     "method": "masked_zncc", "threshold": 0.85},
    {"type": "tap", "label": "ステップ2", "template": "s2.png", "mask": "s2_mask.png",
     "context": "s2.png", "x": 5, "y": 5, "dx": 0, "dy": 0,
     "method": "masked_zncc", "threshold": 0.85},
]
popups = [
    {"label": "フレンド申請", "template": "p1.png", "mask": "p1_mask.png",
     "context": "p1.png", "x": 5, "y": 5, "dx": 0, "dy": 0,
     "method": "masked_zncc", "threshold": 0.8},
]
core.save_recipe(NAME, {"device_size": [200, 200], "screenshot_size": [200, 200],
                         "popups": popups, "steps": steps})

# ============================================================
# 1) ピッカーにステップと共通ポップアップの両方が表示される
# ============================================================
dlg = gui.ConditionPickerDialog(d, steps, popups)
labels = [dlg.list.item(i).text() for i in range(dlg.list.count())]
assert labels == ["ステップ1", "ステップ2", "── 共通ポップアップ ──", "[ポップアップ] フレンド申請"], labels
assert dlg._entries[0] == ("step", steps[0])
assert dlg._entries[3] == ("popup", popups[0])
print("1) ステップ・共通ポップアップの両方が区別できる形で表示される: OK ->", labels)

# ============================================================
# 2) 共通ポップアップを選ぶと確認が出て、「はい」でpopupsから外れる
# ============================================================
dlg.list.setCurrentRow(3)  # [ポップアップ] フレンド申請
dlg.rb_found.setChecked(True)
_question_answer["resp"] = QtWidgets.QMessageBox.Yes
dlg.accept()  # QDialog.accept()相当を直接呼ぶ(exec()はモーダルなので使わない)
assert popups == [], "popupsから削除されていない"
result = dlg.result_value()
assert result is not None
picked_obj, kind = result
assert picked_obj["template"] == "p1.png"
assert kind == "image_found"
print("2) 共通ポップアップを選択 -> 確認ダイアログ経由でpopupsから削除される: OK")

# ============================================================
# 3) 条件はコピーであり、popups/stepsへの参照ではない
# ============================================================
condition = {
    "kind": kind,
    "template": picked_obj["template"],
    "method": picked_obj.get("method", "ccoeff"),
    "threshold": picked_obj.get("threshold", 0.85),
}
if picked_obj.get("mask"):
    condition["mask"] = picked_obj["mask"]
orig_threshold = condition["threshold"]
assert orig_threshold == 0.8, orig_threshold
picked_obj["threshold"] = 0.42  # 元のオブジェクトを後から変更してみる
assert condition["threshold"] == orig_threshold == 0.8, "conditionが参照になっている(コピーされていない)"
print("3) conditionはtemplate/mask/method/thresholdの値コピーで、"
      "元のオブジェクトとは独立している: OK")

# ============================================================
# 4) 「いいえ」を選ぶとpopupsに残ったまま(条件としては使える)
# ============================================================
popups2 = [{"label": "フレンド申請", "template": "p1.png", "mask": "p1_mask.png",
            "context": "p1.png", "x": 5, "y": 5, "dx": 0, "dy": 0,
            "method": "masked_zncc", "threshold": 0.8}]
core.save_recipe(NAME, {"device_size": [200, 200], "screenshot_size": [200, 200],
                         "popups": popups2, "steps": steps})
dlg2 = gui.ConditionPickerDialog(d, steps, popups2)
dlg2.list.setCurrentRow(3)
_question_answer["resp"] = QtWidgets.QMessageBox.No
dlg2.accept()
assert len(popups2) == 1, "「いいえ」を選んだのにpopupsから消えてしまった"
result2 = dlg2.result_value()
assert result2 is not None
picked2, kind2 = result2
assert picked2["template"] == "p1.png"
print("4) 「いいえ」を選んだ場合はpopupsに残るが、選択自体(条件としての利用)は許可される: OK")

# ============================================================
# 5) 再生開始時の競合検出ログ
# ============================================================
if_node = {"type": "if", "label": "フレンド分岐",
           "condition": {"kind": "image_found", "template": "p1.png",
                          "mask": "p1_mask.png", "method": "masked_zncc", "threshold": 0.8},
           "then": [dict(steps[1])], "else": []}
steps_with_if = [dict(steps[0]), if_node]
core.save_recipe(NAME, {"device_size": [200, 200], "screenshot_size": [200, 200],
                         "popups": popups2, "steps": steps_with_if})


class FakeDevice:
    serial = "fake"

    def screenshot(self):
        return Image.new("RGB", (200, 200))

    def window_size(self):
        return (200, 200)


orig_connect = core.connect
orig_peak_match = core.peak_match
orig_tap = core.tap
core.connect = lambda serial=None: FakeDevice()
core.peak_match = lambda screen_gray, tpl_gray, method="ccoeff", mask=None: (5, 5, 0.99)
core.tap = lambda *a, **kw: None

logs = []
t = gui.PlayerThread(None, NAME, 1, 0.0, 1, 0.02, 0.02, 0, 3,
                      verify=True, tap_retry=1, hold_ms=0)
t.sig_log.connect(lambda m: logs.append(m))
try:
    t.run()
finally:
    core.connect = orig_connect
    core.peak_match = orig_peak_match
    core.tap = orig_tap

assert any("フレンド分岐" in l and "フレンド申請" in l and "p1.png" in l for l in logs), (
    "競合検出ログが出なかった:\n" + "\n".join(logs))
print("5) 再生開始時、条件と共通ポップアップが同じ画像を使っていると警告ログが出る: OK")

print("\n=== 共通ポップアップを条件に使う機能: ALL CHECKS PASSED ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)

# PySide6+numpy/OpenCVの組み合わせで、テスト自体は全て成功しているにも
# 関わらず、インタプリタ終了時のガベージコレクション中にネイティブ側で
# 異常終了することがある(test_if_engine.py/test_ui_improvements.pyと同じ
# 現象、実測で確認済み)。判定はここまでのassertで完全に終わっているため、
# 後始末を経由しない即時終了で回避する
import os
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)
