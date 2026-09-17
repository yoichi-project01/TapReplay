import sys, pathlib, tempfile, json, shutil
import numpy as np

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_"))

import core
core.RECIPES = TEST_ROOT

from PySide6 import QtWidgets
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

NAME = "test_popup_repeat"
core.recipe_dir(NAME)  # creates the folder

t = gui.PlayerThread(
    None, NAME, 1, 0.0, 300, 1.2, 1.5, 6, 3,
    verify=True, tap_retry=3, hold_ms=0,
)
t._serial = "dummy"
t.sw, t.sh = 1080, 2340
t._current_cycle = 1

fake_popup = {
    "label": "ポップアップ1",
    "x": 384, "y": 1419, "dx": 0, "dy": 0,
    "_gray": np.zeros((100, 200), dtype=np.uint8),
}
gray = np.zeros((2340, 1080), dtype=np.uint8)

t._find_best_match = lambda g, popups: (0, fake_popup, 384, 1419, 0.99, "masked_zncc", 0.9, [])
core.tap = lambda *a, **kw: None  # 端末には一切送らない(このテストでは検証したいのはカウント/保存ロジックのみ)

recipe_dir = core.recipe_dir(NAME)


def popup_files():
    return sorted(p.name for p in recipe_dir.glob("popup_repeat_*.png"))


# --- ケース1: 6回連続検知(閾値3を超えるのは4,5,6回目 → 保存されるのは3枚まで) ---
results = []
for i in range(6):
    r = t._dismiss_popup_if_any(gray, [fake_popup], f"タップ{i%4 + 1}")
    results.append(r)

assert all(results), "検知は毎回Trueを返すはず"
assert t._popup_repeat_counts["ポップアップ1"] == 6, t._popup_repeat_counts
files_after_6 = popup_files()
print("6回検知後の画像枚数:", len(files_after_6), files_after_6)
assert len(files_after_6) == gui.PlayerThread.POPUP_REPEAT_MAX_SCREENSHOTS, \
    f"上限{gui.PlayerThread.POPUP_REPEAT_MAX_SCREENSHOTS}枚のはずが{len(files_after_6)}枚"

failures = core.load_failures(NAME)
repeat_entries = [f for f in failures if f.get("kind") == "popup_repeat"]
print("failures.jsonl中のpopup_repeatエントリ数:", len(repeat_entries))
assert len(repeat_entries) == gui.PlayerThread.POPUP_REPEAT_MAX_SCREENSHOTS
for e in repeat_entries:
    assert e["cycle"] == 1
    assert e["popup_label"] == "ポップアップ1"
    assert e["count"] > gui.PlayerThread.POPUP_REPEAT_ALERT_THRESHOLD
    assert "waiting_step_label" in e
print("記録内容OK:", repeat_entries[-1])

# --- ケース2: さらに検知を続けても画像は増えない(上限固定) ---
for i in range(4):
    t._dismiss_popup_if_any(gray, [fake_popup], "タップ1")
files_after_more = popup_files()
assert len(files_after_more) == gui.PlayerThread.POPUP_REPEAT_MAX_SCREENSHOTS, \
    f"上限を超えて増えてしまった: {len(files_after_more)}枚"
print("追加検知後も画像枚数は上限のまま:", len(files_after_more))

# --- ケース3: 新しい周回(カウントリセット)なら、また1回目から数える ---
t._popup_repeat_counts = {}
t._popup_repeat_saved = {}
t._current_cycle = 2
r = t._dismiss_popup_if_any(gray, [fake_popup], "タップ1")
assert t._popup_repeat_counts["ポップアップ1"] == 1
files_after_reset = popup_files()
assert len(files_after_reset) == gui.PlayerThread.POPUP_REPEAT_MAX_SCREENSHOTS, \
    "1回だけの検知では新規画像が増えないはず"
print("周回リセット後、1回検知しただけでは画像が増えないことを確認:", len(files_after_reset))

# --- ケース4: 正常時(1〜2回で収まる)は全く新規ファイルを作らない別レシピで確認 ---
NAME2 = "test_popup_normal"
core.recipe_dir(NAME2)
t2 = gui.PlayerThread(None, NAME2, 1, 0.0, 300, 1.2, 1.5, 6, 3, verify=True, tap_retry=3, hold_ms=0)
t2._serial = "dummy"
t2.sw, t2.sh = 1080, 2340
t2._current_cycle = 1
t2._find_best_match = lambda g, popups: (0, fake_popup, 384, 1419, 0.99, "masked_zncc", 0.9, [])
recipe_dir2 = core.recipe_dir(NAME2)
t2._dismiss_popup_if_any(gray, [fake_popup], "タップ1")
t2._dismiss_popup_if_any(gray, [fake_popup], "タップ1")
extra_files = list(recipe_dir2.glob("popup_repeat_*.png"))
has_failures = (recipe_dir2 / "failures.jsonl").exists()
print("正常時(2回のみ)の余計なファイル:", extra_files, "failures.jsonl存在:", has_failures)
assert not extra_files
assert not has_failures

print("=== ALL CHECKS PASSED ===")

shutil.rmtree(TEST_ROOT, ignore_errors=True)
