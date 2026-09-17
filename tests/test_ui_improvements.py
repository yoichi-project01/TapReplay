import sys, pathlib, shutil, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_ui_"))

import core
core.RECIPES = TEST_ROOT / "recipes"
core.RECIPES.mkdir(parents=True, exist_ok=True)
core.SETTINGS_PATH = TEST_ROOT / "settings.ini"

from PySide6 import QtWidgets, QtCore
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui

QtWidgets.QMessageBox.information = staticmethod(lambda *a, **kw: None)
QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **kw: None)
QtWidgets.QMessageBox.question = staticmethod(lambda *a, **kw: QtWidgets.QMessageBox.Yes)

# ============================================================
# 1) 設定の保存・復元
# ============================================================
w1 = gui.MainWindow()
w1.sp_w.setValue(321)
w1.sp_h.setValue(111)
w1.sp_loops.setValue(42)
w1.sp_thr_offset.setValue(0.07)
w1.sp_to.setValue(123)
w1.sp_after.setValue(3.3)
w1.sp_poll.setValue(2.2)
w1.sp_fail.setValue(9)
w1.sp_hold.setValue(77)
w1.sp_jitter.setValue(13)
w1.ck_verify.setChecked(False)
w1.ed_serial.setText("ABC123SERIAL")
w1.cmb_recipe.setCurrentText("my_test_recipe")
app.processEvents()
w1.settings.sync()

w2 = gui.MainWindow()
assert w2.sp_w.value() == 321
assert w2.sp_h.value() == 111
assert w2.sp_loops.value() == 42
assert abs(w2.sp_thr_offset.value() - 0.07) < 1e-6
assert w2.sp_to.value() == 123
assert abs(w2.sp_after.value() - 3.3) < 1e-6
assert abs(w2.sp_poll.value() - 2.2) < 1e-6
assert w2.sp_fail.value() == 9
assert w2.sp_hold.value() == 77
assert w2.sp_jitter.value() == 13
assert w2.ck_verify.isChecked() is False
assert w2.ed_serial.text() == "ABC123SERIAL"
assert w2.cmb_recipe.currentText() == "my_test_recipe"
print("1) 全設定(記録/再生タブ・シリアル・最後のレシピ名)が再起動後も復元される: OK")

# ============================================================
# 2) 設定を初期値に戻す
# ============================================================
w2.on_reset_settings()
assert w2.sp_w.value() == 200
assert w2.sp_h.value() == 100
assert w2.sp_loops.value() == 0
assert w2.sp_thr_offset.value() == 0.0
assert w2.sp_to.value() == 300
assert abs(w2.sp_after.value() - 1.2) < 1e-6
assert abs(w2.sp_poll.value() - 1.5) < 1e-6
assert w2.sp_fail.value() == 3
assert w2.sp_hold.value() == 0
assert w2.sp_jitter.value() == 6
assert w2.ck_verify.isChecked() is True
# レシピ名・シリアルはリセット対象外
assert w2.ed_serial.text() == "ABC123SERIAL"
assert w2.cmb_recipe.currentText() == "my_test_recipe"
print("2) 「設定を初期値に戻す」で数値設定のみ初期値に戻り、"
      "シリアル・レシピ名は維持される: OK")

# リセット後の値も再起動で保持されること(自動保存の再確認)
w2.settings.sync()
w3 = gui.MainWindow()
assert w3.sp_w.value() == 200
assert w3.sp_loops.value() == 0
print("2b) リセット後の値も次回起動時に保持される: OK")

# ============================================================
# 3) 未接続時の自動接続(_ensure_connected)
# ============================================================
calls = {"n": 0}
orig_connect = core.connect


def fake_connect_fail(serial=None):
    calls["n"] += 1
    raise RuntimeError("端末が見つかりません(テスト用)")


core.connect = fake_connect_fail
try:
    assert w3._connected is False
    ok = w3._ensure_connected()
    assert ok is False
    assert calls["n"] == 1, "自動接続が試みられていない"
finally:
    core.connect = orig_connect
print("3) 未接続状態でボタン相当の_ensure_connected()を呼ぶと自動で接続を"
      "試み、失敗時はFalseを返す(例外を投げない): OK")


class FakeDevice:
    serial = "fakeserial"
    def device_info(self):
        pass
    device_info = {"model": "FakeModel"}
    def window_size(self):
        return (100, 200)


def fake_connect_ok(serial=None):
    calls["n"] += 1
    return FakeDevice()


core.connect = fake_connect_ok
try:
    ok = w3._ensure_connected()
    assert ok is True
    assert w3._connected is True
    assert "FakeModel" in w3.lbl_stat.text()
finally:
    core.connect = orig_connect
print("4) 接続に成功すると_connected=Trueになり、状態表示にモデル名が出る: OK")

# 一度接続済みなら、次に呼んでも再接続を試みない(既に繋がっているので)
before_calls = calls["n"]
core.connect = fake_connect_fail
try:
    ok = w3._ensure_connected()
    assert ok is True
    assert calls["n"] == before_calls, "既に接続済みなのに再接続を試みている"
finally:
    core.connect = orig_connect
print("5) 既に接続済みなら_ensure_connected()は再接続を試みない: OK")

# ============================================================
# 6) 接続状態の色分け
# ============================================================
w3._set_connection_status("disconnected", "未接続")
assert "777777" in w3.lbl_stat.styleSheet()
w3._set_connection_status("connected", "接続: X")
assert "1a7a1a" in w3.lbl_stat.styleSheet()
w3._set_connection_status("error", "接続失敗")
assert "b00000" in w3.lbl_stat.styleSheet()
print("6) 接続状態に応じて色分け(灰/緑/赤)される: OK")

# ============================================================
# 7) 再生中の進捗表示
# ============================================================
# isVisible()は祖先(トップレベルウィンドウ)が実際にshow()されていないと
# 常にFalseを返すため、ここで初めて表示する
w3.show()
app.processEvents()
w3.lbl_progress.setText("")
w3.lbl_timing.setText("")
w3.progress_bar.setVisible(False)
w3._last_counts = (0, 0, 0)

w3.on_cycle_update(3, 1, 0)
w3.on_progress_update(5, 20, 125.4, 25.08)
assert w3.lbl_progress.text() == "5 / 20 周目（成功3 / 失敗1 / 不完全0）", w3.lbl_progress.text()
assert "2分5秒" in w3.lbl_timing.text() or "125" in w3.lbl_timing.text()
assert w3.progress_bar.isVisible()
assert w3.progress_bar.value() == 5
assert w3.progress_bar.maximum() == 20
print("7) 再生中の進捗表示(周回・成功失敗数・経過時間・プログレスバー): OK ->",
      w3.lbl_progress.text(), "/", w3.lbl_timing.text())

# 無限ループ(0)の場合はバーを表示しない
w3.on_cycle_update(1, 0, 0)
w3.on_progress_update(7, 0, 300.0, 42.8)
assert w3.lbl_progress.text() == "7 周目（成功1 / 失敗0 / 不完全0）", w3.lbl_progress.text()
assert not w3.progress_bar.isVisible()
print("8) 無限周回(0)の場合は「N周目」表示のみでプログレスバーは非表示: OK ->",
      w3.lbl_progress.text())

# ============================================================
# 9) format_duration
# ============================================================
assert gui.format_duration(5) == "5秒"
assert gui.format_duration(65) == "1分5秒"
assert gui.format_duration(3661) == "1時間1分"
print("9) format_duration: OK")

print("\n=== UI改善(設定保存・自動接続・進捗表示): ALL CHECKS PASSED ===")

# MainWindowは起動時に自動接続用のバックグラウンドQThread
# (DeviceConnectThread)を起動する。close()を呼ばないままスクリプトが
# 終了すると、実行中のQThreadがインタプリタ終了時のガベージコレクションで
# 破棄されクラッシュすることがある(実測で確認済み)。closeEvent側で
# そのスレッドの完了を待つよう修正済みなので、ここでは単に閉じるだけでよい
for w in (w1, w2, w3):
    w.close()

shutil.rmtree(TEST_ROOT, ignore_errors=True)

# PySide6のウィジェット・QThread・numpy配列を複数扱った後、インタプリタ終了時の
# 後片付け中にネイティブ側で異常終了することがある(実測で確認済み。
# test_if_engine.pyと同じ現象)。判定はここまでのassertで完全に終わっている
# ため、後始末を経由しない即時終了で回避する
import os
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)
