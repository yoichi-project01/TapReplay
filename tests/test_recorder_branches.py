"""RecorderDialog が if/else(分岐)を含むレシピを壊さないことを確認する
回帰テスト。実機不要、pytest等も不要(標準ライブラリとassertのみ)。

実行方法: python tests/test_recorder_branches.py

背景: steps がフラットな配列である前提のコードのうち、RecorderDialogの
「続きから記録」は特に被害が大きい(レシピ全体の分岐構造を壊しうる)。
ここではその根本原因だった4件(接続直後の画面描画がifノードでクラッシュ
する/新規ステップのファイル名採番が分岐の中のファイルと衝突する/
「一つ戻す」が既存のifブロックを中身ごと消しうる/最初からやり直す時の
未使用ファイル判定が分岐の中を見落とす)を再現・確認する。
"""
import sys
import json
import shutil
import pathlib
import tempfile

import numpy as np
from PIL import Image

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="tapreplay_test_recorder_branches_"))

import core  # noqa: E402
core.RECIPES = TEST_ROOT
from PySide6 import QtWidgets, QtCore, QtGui  # noqa: E402
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
import gui  # noqa: E402

# 実機のデスクトップ上に本物のモーダルダイアログが出て自動テストが
# 止まってしまわないよう、情報/警告ダイアログはログ出力だけにする
QtWidgets.QMessageBox.information = staticmethod(lambda *a, **kw: None)
QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **kw: print("  [warning]", a[1:3]))
# _load_existingの確認ダイアログ: テストごとに応答を差し替えられるようにする
_answer = {"resp": QtWidgets.QMessageBox.Yes}
QtWidgets.QMessageBox.question = staticmethod(lambda *a, **kw: _answer["resp"])


def make_recipe(name, root_steps_count=5, branch=True):
    """5つのタップを記録し、2番目3番目をifのthenへまとめた(元は5ステップ分の
    ファイルを消費しているが、ルート階層は4件しかない)状態のレシピを作る"""
    d = core.recipe_dir(name)
    full_w, full_h = 200, 300
    ctx = np.random.RandomState(1).randint(0, 256, (full_h, full_w), dtype=np.uint8)
    Image.fromarray(np.stack([ctx] * 3, axis=-1)).save(d / "ctx.png")

    def tap(i, label):
        arr = np.random.RandomState(100 + i).randint(0, 256, (10, 10), dtype=np.uint8)
        Image.fromarray(arr).save(d / f"step_{i:02d}.png")
        Image.fromarray(np.full((10, 10), 255, dtype=np.uint8)).save(d / f"mask_{i:02d}.png")
        Image.fromarray(np.stack([ctx] * 3, axis=-1)).save(d / f"context_{i:02d}.png")
        return {"type": "tap", "label": label, "template": f"step_{i:02d}.png",
                "mask": f"mask_{i:02d}.png", "context": f"context_{i:02d}.png",
                "x": 10 + i, "y": 10 + i, "dx": 0, "dy": 0,
                "method": "masked_zncc", "threshold": 0.85}

    taps = [tap(i, f"タップ{i}") for i in range(1, root_steps_count + 1)]
    if branch:
        steps = [
            taps[0],
            {"type": "if", "label": "分岐",
             "condition": {"kind": "image_found", "template": taps[1]["template"],
                            "mask": taps[1]["mask"], "method": "masked_zncc", "threshold": 0.85},
             "then": [taps[1], taps[2]], "else": []},
        ] + taps[3:]
    else:
        steps = taps
    core.save_recipe(name, {"device_size": [full_w, full_h], "screenshot_size": [full_w, full_h],
                             "popups": [], "steps": steps})
    return d


# masked_zncc は切り抜いた範囲に実際の絵柄(分散)が無いと
# 「テンプレートのマスク内に絵柄がありません」で例外になるため、
# 単色ではなくノイズ入りの画像を使う。複数フレームで完全に同じ画像を
# 返すことで、フレーム間の分散(=不安定と判定される部分)はゼロのまま、
# 絵柄自体の分散(=有効画素率の判定に使われる)は確保できる
_BASE_FRAME = Image.fromarray(
    np.random.RandomState(7).randint(0, 256, (300, 200, 3), dtype=np.uint8))


class FakeDevice:
    serial = "fake"

    def screenshot(self):
        return _BASE_FRAME

    def window_size(self):
        return (200, 300)


def new_dialog(name, retake_label=None):
    """RecorderDialog.__init__は実機接続(バックグラウンドスレッド)を
    伴うため、テストでは__new__してから必要な属性だけ直接組み立てる"""
    dlg = gui.RecorderDialog.__new__(gui.RecorderDialog)
    QtWidgets.QDialog.__init__(dlg)
    dlg.serial = "fake"
    dlg.name = name
    dlg.tpl_w, dlg.tpl_h = 40, 40
    dlg.d = FakeDevice()
    dlg.sw, dlg.sh = 200, 300
    dlg.shot_w, dlg.shot_h = 200, 300
    dlg._connect_thread = None
    dlg.dir = core.recipe_dir(name)
    dlg.steps = []
    dlg.popups = []
    dlg._history = []
    dlg._purge_on_save = None
    dlg._dirty = False
    dlg.pil = _BASE_FRAME
    dlg.scale = 1.0
    dlg._loaded_device_size = None
    dlg._retake_step = None
    dlg._retake_seq = 0
    dlg.img = gui.ClickableLabel()
    dlg.list_steps_edit = gui.ReorderableListWidget()
    dlg.list_popups_edit = QtWidgets.QListWidget()
    dlg.log = QtWidgets.QPlainTextEdit()
    dlg.ck_popup_mode = QtWidgets.QCheckBox()
    dlg.ck_send = QtWidgets.QCheckBox()
    dlg.sp_delay = QtWidgets.QDoubleSpinBox()
    dlg.sp_delay.setValue(0.01)
    dlg._load_existing(auto_continue=(retake_label is not None))
    dlg._refresh_lists()
    if retake_label is not None:
        dlg._arm_retake_by_label(retake_label)
    return dlg


def main():
    # ============================================================
    # 1) 続きから記録: 分岐構造がそのまま保たれる
    # ============================================================
    name1 = "recorder_branch_continue"
    d1 = make_recipe(name1)
    _answer["resp"] = QtWidgets.QMessageBox.Yes
    dlg1 = new_dialog(name1)
    assert len(dlg1.steps) == 4, dlg1.steps  # タップ1, if, タップ4, タップ5
    assert dlg1.steps[1]["type"] == "if"
    assert [n["label"] for n in dlg1.steps[1]["then"]] == ["タップ2", "タップ3"]
    print("1) 続きから記録 -> ルート4件、if(then=タップ2,3)がそのまま保持: OK")

    # 一覧表示: [if]付きで表示され、編集不可になっている
    if_item = dlg1.list_steps_edit.item(1)
    assert if_item.text() == "[if] 分岐", if_item.text()
    assert not (if_item.flags() & QtCore.Qt.ItemIsEditable)
    print("1b) 一覧に[if]として表示され、編集不可: OK")

    # 接続直後に呼ばれるrefresh()/_render_current_pil()が、ifノードのマーカー
    # 描画でKeyError('x')にならず、何もクリックしなくても画面表示できること
    dlg1.refresh()
    print("1c) 接続直後のrefresh()(画面上へのマーカー描画)がifノードでもクラッシュしない: OK")

    # ============================================================
    # 2) 新規ステップはルート末尾に追加され、ファイル名が衝突しない
    #    (ルートは4件だが、ファイルは既にstep_05.pngまで使われている)
    # ============================================================
    next_idx = dlg1._next_free_index("step_", "context_", "mask_")
    assert next_idx == 6, next_idx  # len(steps)+1(=5)ではなく、実ファイル最大(5)+1
    dlg1._handle_capture_point(50, 60)
    assert len(dlg1.steps) == 5
    new_step = dlg1.steps[-1]
    assert new_step["label"] == "タップ6", new_step["label"]
    assert new_step["template"] == "step_06.png", new_step["template"]
    # 既存のstep_05.png(タップ5、まだ使われている)が上書きされていないこと
    step5_after = np.array(Image.open(d1 / "step_05.png"))
    assert step5_after.shape == (10, 10)
    print("2) 新規ステップはstep_06.pngとして追加され、既存のstep_05.pngは無事: OK ->",
          new_step["template"])

    # ============================================================
    # 3) ifノードは撮り直し・ラベル変更の対象にできない
    # ============================================================
    dlg1.list_steps_edit.setCurrentRow(1)  # [if] 分岐 の行
    dlg1.on_retake_clicked()
    assert dlg1._retake_step is None, "ifノードが撮り直し対象として armed されてしまっている"
    print("3) [if]の行は撮り直し不可(armされない): OK")

    # ============================================================
    # 4) 続きから記録直後の「一つ戻す」は、既存の分岐を消さない(no-op)
    # ============================================================
    name2 = "recorder_branch_undo"
    make_recipe(name2)
    dlg2 = new_dialog(name2)
    before_steps = json.loads(json.dumps(dlg2.steps))  # ディープコピーで保存
    assert dlg2._history == []
    dlg2.undo()  # historyが空なのでno-opのはず
    assert dlg2.steps == before_steps, "「一つ戻す」が既存の分岐構造を消してしまった"
    assert len(dlg2.steps) == 4
    print("4) 続きから記録直後の「一つ戻す」は既存の分岐に影響しない(no-op): OK")

    # 新しく記録した分だけ、一つ戻すで正しく取り消せることも確認
    dlg2._handle_capture_point(20, 20)
    assert len(dlg2.steps) == 5
    dlg2.undo()
    assert len(dlg2.steps) == 4
    assert dlg2.steps[1]["type"] == "if"  # 分岐は無事
    print("4b) 新しく記録した分は「一つ戻す」で正しく取り消せる(既存分岐は無事): OK")

    # ============================================================
    # 5) 保存(save)で分岐構造がrecipe.jsonに正しく反映される
    # ============================================================
    dlg2._handle_capture_point(30, 30)
    dlg2.save()
    saved = json.loads((d1.parent / name2 / "recipe.json").read_text(encoding="utf-8"))
    assert saved["steps"][1]["type"] == "if"
    assert [n["label"] for n in saved["steps"][1]["then"]] == ["タップ2", "タップ3"]
    assert saved["steps"][-1]["label"] == "タップ6"
    print("5) 保存後、recipe.jsonの分岐構造は無事 + 新規ステップも反映: OK")

    # core.load_recipeで実際に読み込めること(再生エンジンと噛み合うこと)も確認
    reloaded = core.load_recipe(name2)
    cursor = core.Cursor(reloaded["steps"])
    assert cursor.current()["label"] == "タップ1"
    print("5b) core.load_recipe / Cursorで問題なく読み込める: OK")

    # ============================================================
    # 6) キャンセル(保存しない)なら recipe.json は変化しない
    # ============================================================
    name3 = "recorder_branch_cancel"
    make_recipe(name3)
    before_json = (core.recipe_dir(name3) / "recipe.json").read_text(encoding="utf-8")
    dlg3 = new_dialog(name3)
    dlg3._handle_capture_point(40, 40)
    assert len(dlg3.steps) == 5
    # saveを呼ばずに終了(実際のUIではcloseEvent/reject相当。ここではファイルへの
    # 書き込みが一切発生していないことだけを確認すれば十分)
    after_json = (core.recipe_dir(name3) / "recipe.json").read_text(encoding="utf-8")
    assert before_json == after_json, "保存していないのにrecipe.jsonが変化した"
    print("6) キャンセル(saveを呼ばない)ならrecipe.jsonは変化しない: OK")

    # ============================================================
    # 7) 「最初からやり直す」: 分岐を含むレシピでも、新規の記録で正しく上書きされる
    # ============================================================
    name4 = "recorder_branch_startover"
    d4 = make_recipe(name4)
    _answer["resp"] = QtWidgets.QMessageBox.No
    dlg4 = new_dialog(name4)
    assert dlg4.steps == [] and dlg4.popups == []
    assert dlg4._purge_on_save is not None and len(dlg4._purge_on_save) > 0
    print("7) 最初からやり直す -> steps/popupsは空、旧ファイルは削除候補に: OK")

    dlg4._handle_capture_point(15, 15)
    # 「最初からやり直す」を選んでも、古いファイル自体は保存(save)するまで
    # 削除しない(キャンセルされた場合に元のレシピを壊さないため)。そのため
    # 新しいステップの採番は、まだディスク上に残っている旧ファイル(step_01〜05)
    # とは衝突しないよう、その先(6)から始まる。1から採番して即座に
    # 旧step_01.pngを上書きしてしまうと、保存前にキャンセルしたときに
    # 元のレシピを壊してしまうため、これも意図した(より安全な)挙動
    new_tpl = dlg4.steps[0]["template"]
    assert new_tpl == "step_06.png", new_tpl
    dlg4.save()
    saved4 = json.loads((d4 / "recipe.json").read_text(encoding="utf-8"))
    assert len(saved4["steps"]) == 1
    # ラベルもファイル名の連番と揃えてある(採番はディスク基準の6なので「タップ6」)。
    # 表示名は自由に変更できるので、これは実害のない見た目上の違いにすぎない
    assert saved4["steps"][0]["label"] == "タップ6", saved4["steps"][0]["label"]
    assert saved4["steps"][0]["template"] == new_tpl
    # 旧ファイル(分岐の中にあったものも含め全て)は、保存時の未使用ファイル
    # 削除(purge)で綺麗に消えている
    remaining = sorted(p.name for p in d4.glob("step_*.png"))
    assert remaining == [new_tpl], remaining
    print("7b) やり直し後に保存 -> 新しい1ステップのみ残り、旧ファイル"
          "(分岐の中の分も含め)は全て削除される: OK ->", remaining)

    print("\n=== RecorderDialog: if/elseを含むレシピの安全な続き記録: ALL CHECKS PASSED ===")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TEST_ROOT, ignore_errors=True)
