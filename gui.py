"""
gui.py  ―  TapReplay 本体 (PySide6)
==========================================
Android 端末の画面操作を記録し、あとから再生する汎用ツール。
アプリの動作テスト(QA)、定型作業の自動化(RPA)、操作補助などに使える。

    pip install uiautomator2 opencv-python numpy pillow PySide6
    python gui.py

使い方の流れ:
    1. 端末を USB 接続し「接続」を押す
    2. レシピ名を入れて「記録開始」→ PC に出た端末画面の上で
       操作したいボタンを順にクリック →「保存して閉じる」
    3. 実行回数（0=無限ループ）を入れて「再生開始」
    4. 止めたいときは「停止」
"""

import io
import json
import time
import shutil
import datetime

import cv2
import numpy as np
from PySide6 import QtWidgets, QtCore, QtGui

import core

# レシピ名はフォルダ名としてそのまま使われるため、パス区切りなどは禁止する
INVALID_NAME_CHARS = '\\/:*?"<>|'

# Windows の予約デバイス名。そのままフォルダ名にするとOSエラーになる
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def is_valid_recipe_name(name):
    if not name or any(c in INVALID_NAME_CHARS for c in name):
        return False
    if name != name.strip(" ."):
        return False  # Windowsは末尾の空白・ピリオドを扱えない
    if name.upper() in _RESERVED_NAMES:
        return False
    return True


def pil_to_qpix(pil_img):
    buf = io.BytesIO()
    pil_img.save(buf, "PNG")
    qimg = QtGui.QImage.fromData(buf.getvalue(), "PNG")
    return QtGui.QPixmap.fromImage(qimg)


class HelpBadge(QtWidgets.QLabel):
    """丸い「?」マーク。カーソルを合わせるとツールチップ、
    クリックすると使い方をダイアログで表示する"""

    def __init__(self, tip, parent=None):
        super().__init__("?", parent)
        self._tip = tip
        self.setObjectName("helpBadge")
        self.setFixedSize(18, 18)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setToolTip(tip)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        # "#helpBadge" で自分自身にだけ絞る。"QLabel {...}" のような
        # 型セレクタだと、このバッジを親にして開くダイアログ内のQLabel
        # (メッセージ本文など)にまでスタイルが伝播し、白文字×青背景で
        # 読みにくくなってしまうため。
        self.setStyleSheet(
            "QLabel#helpBadge {"
            " background-color: #3b78c2;"
            " color: white;"
            " border-radius: 9px;"
            " font-weight: bold;"
            " font-size: 12px;"
            "}"
        )

    def mousePressEvent(self, event):
        QtWidgets.QMessageBox.information(self.window(), "使い方", self._tip)


def help_label(text, tip):
    """ラベル文字列の右に丸い「?」マークを添えたウィジェットを返す"""
    w = QtWidgets.QWidget()
    h = QtWidgets.QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(4)
    h.addWidget(QtWidgets.QLabel(text))
    h.addWidget(HelpBadge(tip))
    h.addStretch(1)
    return w


def with_help(widget, tip):
    """widget(ボタン・チェックボックスなど)の右に丸い「?」マークを添えた
    コンテナウィジェットを返す。widget自体は変更せずそのまま使えるので、
    呼び出し側は元のwidget参照(self.xxxなど)を保持したまま、
    レイアウトにはこの戻り値を追加すること"""
    widget.setToolTip(tip)
    w = QtWidgets.QWidget()
    h = QtWidgets.QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(4)
    h.addWidget(widget, 1)
    h.addWidget(HelpBadge(tip))
    return w


def groupbox_help(tip):
    """グループボックスの直前に置く、右寄せの丸い「?」マークだけの行を返す"""
    row = QtWidgets.QHBoxLayout()
    row.setContentsMargins(0, 0, 4, 0)
    row.addStretch(1)
    row.addWidget(HelpBadge(tip))
    return row


# ============================================ クリックできる画像ラベル
class ClickableLabel(QtWidgets.QLabel):
    clicked = QtCore.Signal(int, int)

    def mousePressEvent(self, e):
        self.clicked.emit(int(e.position().x()), int(e.position().y()))


# ==================================== ドラッグ&ドロップで並び替え可能な一覧
class ReorderableListWidget(QtWidgets.QListWidget):
    """InternalMoveでの並び替え中、Qt内部の実装(行の挿入→削除)により
    itemChanged が「移動前の行番号」のまま一時的に発火することがある
    (PySide6 6.11で確認)。これをitemChanged側のハンドラで見分けるのは
    難しいため、dropEvent の開始～終了を dropping フラグで明示し、
    ハンドラ側でその間は無視できるようにする"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dropping = False

    def dropEvent(self, event):
        self.dropping = True
        try:
            super().dropEvent(event)
        finally:
            self.dropping = False


# ============================================ 記録ダイアログ（クリック式）
class RecorderDialog(QtWidgets.QDialog):
    def __init__(self, serial, name, tpl_w, tpl_h, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"記録: {name}")
        self.serial = serial
        self.name = name
        self.tpl_w = tpl_w
        self.tpl_h = tpl_h
        self.d = core.connect(serial)
        self.sw, self.sh = self.d.window_size()
        self.dir = core.recipe_dir(name)
        self.steps = []
        self.popups = []
        self._history = []  # 記録順の "step"/"popup" 履歴(一つ戻す用)
        # 「最初からやり直す」を選んだ時、削除対象の古いファイル名を
        # 保持しておく集合。保存(save)するまでは実際には削除しない
        # (キャンセルされた場合に元のレシピを壊さないため)
        self._purge_on_save = None
        self._dirty = False  # 保存していない変更があるか(閉じる時の確認用)
        self.pil = None
        self.scale = 1.0

        root = QtWidgets.QHBoxLayout(self)

        # 左: 端末画面
        self.img = ClickableLabel()
        self.img.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        self.img.clicked.connect(self.on_click)
        root.addWidget(self.img)

        # 右: 操作パネル
        side = QtWidgets.QVBoxLayout()
        side.addWidget(QtWidgets.QLabel(
            "画面の上で、操作したいボタンを\n実行したい順にクリック"))
        self.ck_send = QtWidgets.QCheckBox("クリックを端末にも送る(画面を進める)")
        self.ck_send.setChecked(True)
        side.addWidget(with_help(
            self.ck_send,
            "ONにすると、記録のためにクリックした位置に実際のタップも端末へ送信し、"
            "画面を先に進めます。OFFにすると記録だけ行うので、端末の画面は"
            "自分の手で操作して進める必要があります。"))

        self.sp_delay = QtWidgets.QDoubleSpinBox()
        self.sp_delay.setRange(0.3, 10); self.sp_delay.setValue(1.5)
        drow = QtWidgets.QHBoxLayout()
        drow.addWidget(help_label(
            "送信後に画面更新するまで秒",
            "端末にタップを送信してから、次のクリックを受け付けるまでの待ち時間(秒)。"
            "画面の反応が遅いアプリでは長めにしてください。"))
        drow.addWidget(self.sp_delay)
        side.addLayout(drow)

        b_refresh = QtWidgets.QPushButton("画面更新")
        b_refresh.clicked.connect(self.refresh)
        b_undo = QtWidgets.QPushButton("一つ戻す")
        b_undo.clicked.connect(self.undo)
        b_save = QtWidgets.QPushButton("保存して閉じる")
        b_save.clicked.connect(self.save)
        b_cancel = QtWidgets.QPushButton("キャンセル")
        # reject()ではなくclose()にすることで、ウィンドウの「×」と挙動を揃える
        # (closeEvent側で未保存の変更があれば確認する)
        b_cancel.clicked.connect(self.close)
        for b, tip in (
            (b_refresh, "端末の現在の画面を撮り直して表示を更新します。"),
            (b_undo, "直前に記録したステップ、または共通ポップアップを1つ取り消します。"),
            (b_save, "ここまで記録した内容をレシピとして保存し、記録ウィンドウを閉じます。"),
            (b_cancel, "記録した内容を保存せずに記録ウィンドウを閉じます。"),
        ):
            side.addWidget(with_help(b, tip))

        side.addWidget(help_label(
            "記録したステップ（ダブルクリックで名前変更／ドラッグで順番変更）",
            "このレシピで記録済みの操作ステップの一覧です。上から順番に実行されます。"
            "項目をダブルクリックすると「タップ1」のような名前を自由に変更でき、"
            "ドラッグ＆ドロップで実行順を入れ替えられます。"))
        self.list_steps_edit = ReorderableListWidget()
        self.list_steps_edit.setMaximumHeight(120)
        self.list_steps_edit.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.list_steps_edit.itemChanged.connect(self.on_step_label_edited)
        self.list_steps_edit.model().rowsMoved.connect(self.on_steps_reordered)
        side.addWidget(self.list_steps_edit)

        self.ck_popup_mode = QtWidgets.QCheckBox(
            "共通ポップアップとして記録\n(広告や「フレンド申請」等、順序を問わず割り込んだら閉じる用)")
        side.addWidget(with_help(
            self.ck_popup_mode,
            "ONの状態でクリックすると、そのステップは通常の順番の一部ではなく"
            "「いつ現れても閉じる」共通ポップアップとして登録されます。"
            "フレンド申請やイベント告知など、不定期に割り込んでくる画面の"
            "OK/閉じるボタンに使ってください。"))

        side.addWidget(help_label(
            "共通ポップアップ一覧（ダブルクリックで名前変更）",
            "「共通ポップアップとして記録」した画像の一覧です。再生中は、今どの"
            "ステップを待っていてもこれらの画像が見えたら優先して閉じます。"))
        self.list_popups_edit = QtWidgets.QListWidget()
        self.list_popups_edit.setMaximumHeight(90)
        self.list_popups_edit.itemChanged.connect(self.on_popup_label_edited)
        side.addWidget(self.list_popups_edit)

        self.log = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True)
        side.addWidget(self.log, 1)
        root.addLayout(side)

        self._load_existing()
        self._refresh_lists()
        self.refresh()

    def _refresh_lists(self):
        self.list_steps_edit.blockSignals(True)
        self.list_steps_edit.clear()
        for s in self.steps:
            item = QtWidgets.QListWidgetItem(s["label"])
            item.setFlags(item.flags() | QtCore.Qt.ItemIsEditable)
            item.setData(QtCore.Qt.UserRole, s)
            self.list_steps_edit.addItem(item)
        self.list_steps_edit.blockSignals(False)

        self.list_popups_edit.blockSignals(True)
        self.list_popups_edit.clear()
        for p in self.popups:
            item = QtWidgets.QListWidgetItem(p["label"])
            item.setFlags(item.flags() | QtCore.Qt.ItemIsEditable)
            self.list_popups_edit.addItem(item)
        self.list_popups_edit.blockSignals(False)

    def on_steps_reordered(self, *args):
        """ドラッグ＆ドロップで並び替えた後、self.stepsの順番も同期する。
        リスト自体はInternalMoveで既に正しい見た目になっているので、
        ここではPython側のデータだけ同期する(移動中にリストを作り直すと
        Qtの内部状態と衝突する恐れがあるため、clear/再構築はしない)"""
        self.steps = [
            self.list_steps_edit.item(i).data(QtCore.Qt.UserRole)
            for i in range(self.list_steps_edit.count())
        ]
        self._dirty = True
        self._msg("ステップの順番を変更しました")
        self.refresh()

    def on_step_label_edited(self, item):
        if self.list_steps_edit.dropping:
            # ドラッグ&ドロップ中はQtの内部実装により、移動前の行番号を
            # 指したままitemChangedが誤発火することがあるため無視する
            # (実際の並び替え結果はrowsMoved→on_steps_reorderedで同期する)
            return
        idx = self.list_steps_edit.row(item)
        if not (0 <= idx < len(self.steps)):
            return
        new_label = item.text().strip()
        if new_label:
            self.steps[idx]["label"] = new_label
            self._dirty = True
            self._msg(f"step{idx + 1} の名前を「{new_label}」に変更しました")
        else:
            item.setText(self.steps[idx]["label"])  # 空にはできない

    def on_popup_label_edited(self, item):
        idx = self.list_popups_edit.row(item)
        if not (0 <= idx < len(self.popups)):
            return
        new_label = item.text().strip()
        if new_label:
            self.popups[idx]["label"] = new_label
            self._dirty = True
            self._msg(f"popup{idx + 1} の名前を「{new_label}」に変更しました")
        else:
            item.setText(self.popups[idx]["label"])  # 空にはできない

    def _load_existing(self):
        """同名レシピが既にあれば、続きから記録するか確認して読み込む"""
        recipe_file = self.dir / "recipe.json"
        if not recipe_file.exists():
            return
        try:
            data = json.loads(recipe_file.read_text(encoding="utf-8"))
        except Exception as e:
            self._msg(f"!! 既存レシピの読み込みに失敗: {e}")
            return
        prev_steps = data.get("steps", [])
        prev_popups = data.get("popups", [])
        if not prev_steps and not prev_popups:
            return

        resp = QtWidgets.QMessageBox.question(
            self, "既存レシピが見つかりました",
            f"「{self.name}」には既に{len(prev_steps)}ステップ"
            f"・{len(prev_popups)}件の共通ポップアップが記録されています。\n\n"
            "「はい」: プログラムが止まった続きから追加記録する\n"
            "「いいえ」: 最初からやり直す（「保存して閉じる」を押すまでは"
            "元の記録は消えません。キャンセルすれば元のまま残ります）",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        )
        if resp == QtWidgets.QMessageBox.Yes:
            self.steps = prev_steps
            self.popups = prev_popups
            self._history = ([("step", s) for s in self.steps] +
                              [("popup", p) for p in self.popups])
            self._msg(
                f"続きから記録します（ステップ{len(prev_steps)}件・"
                f"共通ポップアップ{len(prev_popups)}件を読み込み済み）")
            dev_size = data.get("device_size")
            if dev_size and list(dev_size) != [self.sw, self.sh]:
                self._msg(
                    f"!! 注意: 記録時({dev_size})と今の画面サイズ"
                    f"({self.sw},{self.sh})が違います")
        else:
            # ここでは削除しない。保存(save)まで遅らせることで、
            # このままキャンセルされた場合に元のレシピを壊さないようにする。
            # 削除対象の候補だけ覚えておき、save()で実際に使われなかった
            # ものだけを消す
            self._purge_on_save = set()
            for pattern in ("step_*.png", "context_*.png",
                             "popup_*.png", "context_popup_*.png"):
                for f in self.dir.glob(pattern):
                    self._purge_on_save.add(f.name)
            self._msg(
                "最初からやり直します。「保存して閉じる」を押すまで元の記録は"
                "残ります（キャンセルすれば元のまま使えます）")

    def _msg(self, m):
        self.log.appendPlainText(m)

    def closeEvent(self, event):
        """ウィンドウの「×」・「キャンセル」共通の終了処理。
        保存していない記録がある場合は破棄してよいか確認する"""
        if self._dirty:
            resp = QtWidgets.QMessageBox.question(
                self, "確認",
                "保存していない記録内容があります。保存せずに閉じますか？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if resp != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
        self.setResult(QtWidgets.QDialog.Rejected)
        event.accept()

    def refresh(self):
        try:
            self.pil = self.d.screenshot()
        except Exception as e:
            self._msg(f"!! スクショ失敗: {e}")
            return
        avail = QtWidgets.QApplication.primaryScreen().availableGeometry()
        maxh = int(avail.height() * 0.85)
        maxw = int(avail.width() * 0.6)
        self.scale = min(1.0, maxh / self.sh, maxw / self.sw)
        disp = self.pil.resize(
            (int(self.sw * self.scale), int(self.sh * self.scale)))
        pix = pil_to_qpix(disp)
        # 記録済みの位置に番号入りマーカーを描く(ステップ=赤, ポップアップ=青)
        painter = QtGui.QPainter(pix)
        painter.setFont(QtGui.QFont("Arial", 14, QtGui.QFont.Bold))
        pen = QtGui.QPen(QtGui.QColor(255, 0, 0)); pen.setWidth(3)
        painter.setPen(pen)
        for i, s in enumerate(self.steps, 1):
            x = int(s["x"] * self.scale); y = int(s["y"] * self.scale)
            painter.drawEllipse(QtCore.QPoint(x, y), 14, 14)
            painter.drawText(x + 16, y + 6, str(i))
        pen = QtGui.QPen(QtGui.QColor(60, 140, 255)); pen.setWidth(3)
        painter.setPen(pen)
        for i, p in enumerate(self.popups, 1):
            x = int(p["x"] * self.scale); y = int(p["y"] * self.scale)
            painter.drawEllipse(QtCore.QPoint(x, y), 14, 14)
            painter.drawText(x + 16, y + 6, f"P{i}")
        painter.end()
        self.img.setPixmap(pix)
        self.img.setFixedSize(pix.size())
        # 更新が終わったのでクリックを再度受け付ける
        self.img.setEnabled(True)

    def on_click(self, lx, ly):
        if self.pil is None:
            return
        self._dirty = True
        rx = int(lx / self.scale)
        ry = int(ly / self.scale)
        ctx = cv2.cvtColor(np.array(self.pil), cv2.COLOR_RGB2BGR)
        cv2.rectangle(ctx,
                      (rx - self.tpl_w // 2, ry - self.tpl_h // 2),
                      (rx + self.tpl_w // 2, ry + self.tpl_h // 2),
                      (0, 0, 255), 4)

        crop_img, (dx, dy) = core.crop(self.pil, rx, ry, self.tpl_w, self.tpl_h)
        is_distinctive = core.is_distinctive(core.to_gray(crop_img))

        if self.ck_popup_mode.isChecked():
            idx = len(self.popups) + 1
            tpl = f"popup_{idx:02d}.png"
            ctx_name = f"context_popup_{idx:02d}.png"
            crop_img.save(self.dir / tpl)
            core.imwrite(self.dir / ctx_name, ctx)
            new_item = {"label": f"ポップアップ{idx}", "template": tpl,
                        "context": ctx_name, "x": rx, "y": ry, "dx": dx, "dy": dy}
            self.popups.append(new_item)
            self._history.append(("popup", new_item))
            self._msg(f"popup{idx}: ({rx},{ry}) → {tpl} (共通ポップアップとして記録)")
        else:
            idx = len(self.steps) + 1
            tpl = f"step_{idx:02d}.png"
            ctx_name = f"context_{idx:02d}.png"
            crop_img.save(self.dir / tpl)
            core.imwrite(self.dir / ctx_name, ctx)
            new_item = {"label": f"タップ{idx}", "template": tpl,
                        "context": ctx_name, "x": rx, "y": ry, "dx": dx, "dy": dy}
            self.steps.append(new_item)
            self._history.append(("step", new_item))
            self._msg(f"step{idx}: ({rx},{ry}) → {tpl}")
        if not is_distinctive:
            self._msg(
                "  !! 注意: この画像はほぼ無地で情報量が少ないです。暗転・読み込み画面"
                "などに誤反応しやすいので、文字や模様が入るよう「切抜き幅／高さ」を"
                "広げるか、別の場所をクリックし直すことをおすすめします")
        self._refresh_lists()

        if self.ck_send.isChecked():
            try:
                core.tap(self.serial or self.d.serial, rx, ry)
                self._msg(f"  端末にタップ送信 → 画面更新までクリック無効…")
            except Exception as e:
                self._msg(f"  !! タップ送信失敗: {e}")
            # 更新が終わるまで誤クリック（古い画面での記録）を防ぐ
            self.img.setEnabled(False)
            QtCore.QTimer.singleShot(
                int(self.sp_delay.value() * 1000), self.refresh)
        else:
            self.refresh()

    def undo(self):
        if not self._history:
            return
        kind, obj = self._history.pop()
        # 並び替え後でも安全なように、位置(pop)ではなく対象そのものを取り除く
        target_list = self.popups if kind == "popup" else self.steps
        try:
            target_list.remove(obj)
        except ValueError:
            pass  # 既に別の操作で消えている場合は何もしない
        for fname in (obj.get("template"), obj.get("context")):
            if fname:
                try:
                    (self.dir / fname).unlink()
                except Exception:
                    pass
        self._dirty = True
        self._msg(f"「{obj['label']}」を取り消しました")
        self._refresh_lists()
        self.refresh()

    def save(self):
        if not self.steps:
            self._msg("!! 1つもクリックされていません")
            return
        if self._purge_on_save:
            # 「最初からやり直す」で保留していた古いファイルのうち、
            # 新しい記録で使われなかったものだけをここで削除する
            # (同じ番号を再利用したファイルは新しい内容で上書き済みなので残す)
            keep = set()
            for item in self.steps + self.popups:
                keep.add(item.get("template"))
                keep.add(item.get("context"))
            removed = 0
            for fname in self._purge_on_save:
                if fname in keep:
                    continue
                try:
                    (self.dir / fname).unlink()
                    removed += 1
                except Exception:
                    pass
            if removed:
                self._msg(f"古い記録のファイルを{removed}件削除しました")
            self._purge_on_save = None
        core.save_recipe(self.name, {
            "device_size": [self.sw, self.sh],
            "popups": self.popups,
            "steps": self.steps,
        })
        self._msg(
            f"保存しました: recipes/{self.name}/ "
            f"({len(self.steps)}ステップ・共通ポップアップ{len(self.popups)}件)")
        self.accept()


# ============================================================ 再生スレッド
class PlayerThread(QtCore.QThread):
    sig_log = QtCore.Signal(str)
    sig_cycle = QtCore.Signal(int, int)   # (成功, 失敗)
    sig_done = QtCore.Signal()

    def __init__(self, serial, name, loops, threshold,
                 step_timeout, after, poll, jitter, max_fail,
                 verify=True, tap_retry=3, hold_ms=0):
        super().__init__()
        self.serial = serial
        self.name = name
        self.loops = loops
        self.threshold = threshold
        self.step_timeout = step_timeout
        self.after = after
        self.poll = poll
        self.jitter = jitter
        self.max_fail = max_fail
        self.verify = verify
        self.tap_retry = max(1, tap_retry)
        self.hold_ms = hold_ms
        self._serial = serial
        self._stop = False
        self._log_file = None

    def stop(self):
        self._stop = True

    def _log(self, msg):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        if self._log_file:
            try:
                self._log_file.write(f"[{stamp}] {msg}\n")
                self._log_file.flush()
            except Exception:
                pass
        self.sig_log.emit(msg)

    def _find_best_match(self, gray, candidates):
        """candidates のうち今の画面に写っているものを探す(一致度が最も高いものを返す)。
        戻り値は (candidates内でのインデックス, 候補dict, cx, cy, val) または
        見つからなければ None。ほぼ無地のテンプレートは暗転画面などに
        誤検知しやすいため対象から除外する"""
        best = None
        for i, s in enumerate(candidates):
            if not core.is_distinctive(s["_gray"]):
                continue
            cx, cy, val = core.match(gray, s["_gray"], self.threshold)
            if cx is not None and (best is None or val > best[4]):
                best = (i, s, cx, cy, val)
        return best

    def _dismiss_popup_if_any(self, gray, popups):
        """共通ポップアップ(広告・フレンド申請等)が写っていれば閉じる。閉じたらTrue"""
        import random
        hit = self._find_best_match(gray, popups)
        if hit is None:
            return False
        _, popup, pcx, pcy, pval = hit
        jx = pcx + popup.get("dx", 0) + random.randint(-self.jitter, self.jitter)
        jy = pcy + popup.get("dy", 0) + random.randint(-self.jitter, self.jitter)
        core.tap(self._serial, jx, jy, self.hold_ms)
        self._log(
            f"    !! 共通ポップアップ「{popup['label']}」を検知(一致{pval:.2f})したので閉じました")
        time.sleep(self.after)
        return True

    def _wait_and_tap(self, d, all_steps, idx, popups):
        """all_steps[idx] の画像が現れるまで待ってタップする。

        見つからない間、レシピ内の"これより後の"ステップ画像が写っていない
        かも探す。見つかればそれをタップし、再生位置をそこまで進める
        (＝そのステップのインデックスを返す)。呼び出し元(run())は、
        戻り値の次のインデックスから再開すること。

        戻り値: 実際にタップできたステップのインデックス
                (通常は idx 自身。この先のステップへ復帰した場合はそのインデックス)
        タイムアウトした場合は TimeoutError を送出する。
        """
        import random
        step = all_steps[idx]
        later_steps = all_steps[idx + 1:]
        deadline = time.time() + self.step_timeout
        best = 0.0
        last_report = time.time()
        while time.time() < deadline:
            if self._stop:
                raise KeyboardInterrupt
            gray = core.to_gray(d.screenshot())

            # 共通ポップアップは、対象ステップの探索より先に毎回チェックする
            # (どのステップを待っていても、順序に関係なく割り込んで閉じる)
            if self._dismiss_popup_if_any(gray, popups):
                continue

            cx, cy, val = core.match(gray, step["_gray"], self.threshold)
            best = max(best, val)

            if cx is not None:
                # 見つかった → タップ。効かなければ押し直す。
                # 成功判定は「押したボタンが画面から消えたか」で見る
                # （背景アニメに惑わされない）
                popup_interrupted = False
                for attempt in range(1, self.tap_retry + 1):
                    jx = cx + step.get("dx", 0) + random.randint(-self.jitter, self.jitter)
                    jy = cy + step.get("dy", 0) + random.randint(-self.jitter, self.jitter)
                    core.tap(self._serial, jx, jy, self.hold_ms)
                    tag = f" [{attempt}回目]" if attempt > 1 else ""
                    self._log(
                        f"    {step['label']}: タップ({jx},{jy}) 一致{val:.2f}{tag}")
                    time.sleep(self.after)
                    if not self.verify:
                        return idx
                    after_gray = core.to_gray(d.screenshot())
                    # ボタンが消えずに残っているように見えても、実は共通ポップアップに
                    # 覆われていて反応していないだけ、というケースがあるため先に確認する
                    if self._dismiss_popup_if_any(after_gray, popups):
                        popup_interrupted = True
                        break
                    ncx, ncy, nval = core.match(after_gray, step["_gray"], self.threshold)
                    if ncx is None:
                        return idx  # ボタンが消えた＝タップ成功、次へ
                    # まだ同じボタンが見えている＝タップが効いていない → 押し直す
                    cx, cy = ncx, ncy
                    self._log(
                        f"    …まだボタンが残っています(一致{nval:.2f})。押し直します")
                if popup_interrupted:
                    continue  # ポップアップを閉じたので対象を探し直す
                self._log(
                    f"    !! {step['label']}: 押しても反応しません。"
                    "「タップ長押しms」を80〜150に上げてみてください")
                raise RuntimeError(
                    f"{step['label']}: {self.tap_retry}回タップしても次の画面に"
                    "遷移しませんでした(同じ場所を押しても無反応)")

            # 対象の画像が見つからない → 想定外の画面(広告・確認ダイアログ等)の
            # 可能性があるので、レシピ内の"これより後の"ステップ画像が
            # 写っていないか探す(前のステップは対象にしない)
            other = self._find_best_match(gray, later_steps)
            if other is not None:
                local_idx, other_step, ocx, ocy, oval = other
                target_idx = idx + 1 + local_idx
                jx = ocx + other_step.get("dx", 0) + random.randint(-self.jitter, self.jitter)
                jy = ocy + other_step.get("dy", 0) + random.randint(-self.jitter, self.jitter)
                core.tap(self._serial, jx, jy, self.hold_ms)
                self._log(
                    f"    !! ステップ{idx + 1}「{step['label']}」をスキップして"
                    f"ステップ{target_idx + 1}「{other_step['label']}」へ進みました"
                    f"(この先の画像を検知・一致{oval:.2f})")
                time.sleep(self.after)
                # 元の対象を待ち続けても二度と現れないので、進んだ先から再開する
                return target_idx

            # まだ見つからない → 数秒おきに現在の一致度を報告
            if time.time() - last_report >= 3:
                last_report = time.time()
                self._log(
                    f"    待機中… {step['label']} 最高一致度 {best:.2f} "
                    f"(しきい値 {self.threshold:.2f})")
            time.sleep(self.poll)

        # タイムアウト → 最高一致度から原因を推定してヒントを出す
        if best >= self.threshold - 0.05:
            hint = "→ ほぼ一致。しきい値を少し下げれば拾えそう"
        elif best >= 0.6:
            hint = "→ 惜しい。切抜きを見直すか、しきい値を下げる"
        else:
            hint = "→ この画面に対象が無い。前のタップが効いていない可能性大"
        raise TimeoutError(
            f"{step['label']} が出現せず (最高一致度 {best:.2f}) {hint}")

    def run(self):
        recipe_dir = core.recipe_dir(self.name)
        log_path = recipe_dir / f"playback_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"
        try:
            self._log_file = open(log_path, "a", encoding="utf-8")
        except Exception:
            self._log_file = None
        try:
            d = core.connect(self.serial)
            self._serial = self.serial or d.serial
            data = core.load_recipe(self.name)
            sw, sh = d.window_size()
            if data.get("device_size") and list(data["device_size"]) != [sw, sh]:
                self._log(
                    f"!! 注意: 記録時({data['device_size']})と画面サイズが違います。"
                    "解像度・向きを合わせてください"
                )
            popups = data.get("popups", [])
            self._log(f"再生開始: {len(data['steps'])}ステップ"
                      f"（共通ポップアップ{len(popups)}件） / "
                      f"{'無限' if self.loops == 0 else self.loops}周")

            ok = ng_total = ng_streak = cycle = 0
            started = time.time()
            while self.loops == 0 or cycle < self.loops:
                if self._stop:
                    break
                cycle += 1
                self._log(f"=== ループ {cycle} ===")
                current_step = None
                try:
                    step_idx = 0
                    n_steps = len(data["steps"])
                    while step_idx < n_steps:
                        current_step = data["steps"][step_idx]
                        reached = self._wait_and_tap(d, data["steps"], step_idx, popups)
                        step_idx = reached + 1
                    ok += 1
                    ng_streak = 0  # 連続失敗カウントは成功したらリセット
                    self.sig_cycle.emit(ok, ng_total)
                    avg = (time.time() - started) / cycle
                    self._log(f"=== ループ {cycle} 完了  平均 {avg:.0f}秒/回 ===")
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    ng_total += 1
                    ng_streak += 1
                    self.sig_cycle.emit(ok, ng_total)
                    self._log(f"!! ループ {cycle} 失敗: {e}")
                    # 失敗の記録自体が失敗しても(端末との接続切れ等)再生は止めない
                    try:
                        safe_reason = "".join(
                            c if c.isalnum() else "_" for c in str(e))[:40]
                        fname = f"error_{datetime.datetime.now():%H%M%S}_{safe_reason}.png"
                        core.imwrite(recipe_dir / fname, core.to_bgr(d.screenshot()))
                        step_label = current_step["label"] if current_step else "?"
                        core.append_failure(self.name, step_label, str(e), fname)
                    except Exception as e2:
                        self._log(f"!! 失敗時のスクリーンショット保存に失敗: {e2}")
                    if ng_streak >= self.max_fail:
                        self._log(f"!! 失敗が{ng_streak}回連続したため停止します")
                        break
                    # back操作自体が失敗しても(接続切れ等)スレッドを落とさず次周へ進む
                    try:
                        d.press("back")
                    except Exception as e2:
                        self._log(f"!! 端末との通信に失敗しました(接続切れの可能性): {e2}")
                    time.sleep(3)
                time.sleep(1)

            total = (time.time() - started) / 60
            self._log(f"終了: 成功{ok} / 失敗{ng_total} / {total:.1f}分")
        except Exception as e:
            self._log(f"!! 再生エラー: {e}")
        finally:
            if self._log_file:
                try:
                    self._log_file.close()
                except Exception:
                    pass
            self.sig_done.emit()


# ================================================================== 画面
class MainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TapReplay — Android 記録＆再生")
        self.resize(560, 700)
        self.serial = None
        self.worker = None

        v = QtWidgets.QVBoxLayout(self)

        # 接続行
        row = QtWidgets.QHBoxLayout()
        self.ed_serial = QtWidgets.QLineEdit()
        self.ed_serial.setPlaceholderText("シリアル(複数端末時のみ)。空でOK")
        self.btn_conn = QtWidgets.QPushButton("接続")
        self.btn_conn.clicked.connect(self.on_connect)
        row.addWidget(QtWidgets.QLabel("端末:"))
        row.addWidget(self.ed_serial, 1)
        row.addWidget(with_help(
            self.btn_conn,
            "USBでつないだAndroid端末に接続します。モデル名と画面サイズが"
            "表示されれば成功です。記録・再生の前に一度押してください。"))
        v.addLayout(row)

        # レシピ名
        row = QtWidgets.QHBoxLayout()
        self.cmb_recipe = QtWidgets.QComboBox()
        self.cmb_recipe.setEditable(True)
        self.cmb_recipe.addItems(core.list_recipes())
        row.addWidget(help_label(
            "レシピ名:",
            "記録・再生の対象となる名前です。recipes/<この名前>/ フォルダに"
            "保存されます。新しい名前を入力すれば新規レシピとして記録できます。"))
        row.addWidget(self.cmb_recipe, 1)
        v.addLayout(row)

        # タブ（記録設定 / 再生設定）
        tabs = QtWidgets.QTabWidget()

        # --- 記録タブ ---
        tab_rec = QtWidgets.QWidget()
        tv = QtWidgets.QVBoxLayout(tab_rec)
        box = QtWidgets.QGroupBox("記録の設定（画面をクリックして記録）")
        g = QtWidgets.QGridLayout(box)
        self.sp_w = QtWidgets.QSpinBox(); self.sp_w.setRange(40, 800); self.sp_w.setValue(200)
        self.sp_h = QtWidgets.QSpinBox(); self.sp_h.setRange(40, 800); self.sp_h.setValue(100)
        g.addWidget(help_label(
            "切抜き幅",
            "クリックした位置を中心に、ボタン画像を切り抜く横幅(ピクセル)です。"
            "大きくすると周囲の文字ごと含められ、似たボタンと区別しやすくなります。"
            "小さすぎるとほぼ無地の画像になり、暗転画面などへの誤検知の原因になります。"), 0, 0)
        g.addWidget(self.sp_w, 0, 1)
        g.addWidget(help_label("高さ", "切り抜く縦幅(ピクセル)です。考え方は「切抜き幅」と同じです。"), 0, 2)
        g.addWidget(self.sp_h, 0, 3)
        self.btn_rec = QtWidgets.QPushButton("記録開始")
        self.btn_rec.clicked.connect(self.on_record)
        g.addWidget(with_help(
            self.btn_rec,
            "上のレシピ名で記録ウィンドウを開きます。既に記録済みのレシピ名を"
            "指定した場合は、続きから追加記録するか選べます。"), 1, 0, 1, 4)
        tv.addLayout(groupbox_help(
            "端末の画面をPC上でクリックして、操作手順(レシピ)を記録します。"))
        tv.addWidget(box)
        tv.addStretch(1)
        tabs.addTab(tab_rec, "記録")

        # --- 再生タブ ---
        tab_play = QtWidgets.QWidget()
        pv = QtWidgets.QVBoxLayout(tab_play)
        box = QtWidgets.QGroupBox("再生の設定")
        g = QtWidgets.QGridLayout(box)
        self.sp_loops = QtWidgets.QSpinBox(); self.sp_loops.setRange(0, 100000); self.sp_loops.setValue(0)
        self.sp_thr = QtWidgets.QDoubleSpinBox(); self.sp_thr.setRange(0.5, 0.99); self.sp_thr.setSingleStep(0.01); self.sp_thr.setValue(0.85)
        self.sp_to = QtWidgets.QSpinBox(); self.sp_to.setRange(5, 3600); self.sp_to.setValue(300)
        self.sp_after = QtWidgets.QDoubleSpinBox(); self.sp_after.setRange(0, 20); self.sp_after.setValue(1.2)
        self.sp_poll = QtWidgets.QDoubleSpinBox(); self.sp_poll.setRange(0.3, 10); self.sp_poll.setValue(1.5)
        self.sp_fail = QtWidgets.QSpinBox(); self.sp_fail.setRange(1, 50); self.sp_fail.setValue(3)
        self.sp_hold = QtWidgets.QSpinBox(); self.sp_hold.setRange(0, 1000); self.sp_hold.setValue(0)
        g.addWidget(help_label(
            "実行回数(0=無限)",
            "再生を何回繰り返すかを指定します。0にすると「停止」を押すまで"
            "無限に繰り返します。"), 0, 0)
        g.addWidget(self.sp_loops, 0, 1)
        g.addWidget(help_label(
            "一致しきい値",
            "記録した画像とどれだけ似ていれば「見つかった」とみなすかの基準"
            "(0〜1、高いほど厳密)。ボタンが見つからない場合は0.80、0.75のように"
            "下げてみてください。ログに出る「最高一致度」が目安になります。"), 0, 2)
        g.addWidget(self.sp_thr, 0, 3)
        g.addWidget(help_label(
            "各ステップ最大待ち秒",
            "1つのステップの画像が現れるまで待つ最大時間(秒)。この時間を"
            "過ぎても見つからなければ、そのステップは失敗として扱われます。"), 1, 0)
        g.addWidget(self.sp_to, 1, 1)
        g.addWidget(help_label(
            "タップ後待ち秒",
            "ボタンをタップしてから、効いたか(消えたか)を確認するまでの"
            "待ち時間(秒)。画面の反応が遅いアプリでは長めにしてください。"), 1, 2)
        g.addWidget(self.sp_after, 1, 3)
        g.addWidget(help_label(
            "確認間隔秒",
            "対象のボタンがまだ現れていないとき、何秒おきに画面を確認しに"
            "いくかの間隔です。"), 2, 0)
        g.addWidget(self.sp_poll, 2, 1)
        g.addWidget(help_label(
            "連続失敗で停止",
            "同じレシピの再生が連続で何回失敗したら、再生全体を停止するかの"
            "回数です。途中で1回でも成功すればこのカウントはリセットされます。"), 2, 2)
        g.addWidget(self.sp_fail, 2, 3)
        g.addWidget(help_label(
            "タップ長押しms(効かない時↑)",
            "タップを押している時間(ミリ秒)。0は瞬間タップ。反応が悪い"
            "アプリではタップしても無反応になりやすいので、80〜150くらいに"
            "上げると改善することがあります。"), 3, 0)
        g.addWidget(self.sp_hold, 3, 1)
        self.ck_verify = QtWidgets.QCheckBox("タップ後に効いたか確認して押し直す")
        self.ck_verify.setChecked(True)
        g.addWidget(with_help(
            self.ck_verify,
            "ONにすると、タップ後にボタンがまだ画面に残っているか確認し、"
            "残っていれば同じ場所を押し直します。OFFにすると1回タップした"
            "だけで確認せずに次のステップへ進みます。"), 3, 2, 1, 2)
        self.btn_play = QtWidgets.QPushButton("再生開始")
        self.btn_play.clicked.connect(self.on_play)
        g.addWidget(with_help(
            self.btn_play, "上で選んだレシピを、この設定で再生します。"), 4, 0, 1, 4)
        pv.addLayout(groupbox_help("記録したレシピを自動で繰り返し実行します。"))
        pv.addWidget(box)
        pv.addStretch(1)
        tabs.addTab(tab_play, "再生")

        # --- 記録内容タブ ---
        tab_content = QtWidgets.QWidget()
        cv = QtWidgets.QVBoxLayout(tab_content)

        btn_refresh_content = QtWidgets.QPushButton("表示を更新（上のレシピ名を対象）")
        btn_refresh_content.clicked.connect(self.refresh_history)
        cv.addWidget(with_help(
            btn_refresh_content,
            "上のレシピ名で記録した内容を、この画面に読み込み直します。"))

        btn_delete_recipe = QtWidgets.QPushButton("このレシピを削除する")
        btn_delete_recipe.setStyleSheet("color: #b00000;")
        btn_delete_recipe.clicked.connect(self.on_delete_recipe)
        cv.addWidget(with_help(
            btn_delete_recipe,
            "レシピをフォルダごと完全に削除します。記録したステップ画像・"
            "共通ポップアップ・失敗履歴もすべて消え、元に戻せません。"))

        box = QtWidgets.QGroupBox("記録したステップ")
        bl = QtWidgets.QVBoxLayout(box)
        self.list_steps = QtWidgets.QListWidget()
        self.list_steps.setIconSize(QtCore.QSize(64, 64))
        cv.addLayout(groupbox_help(
            "このレシピで記録済みの操作ステップの一覧です。上から順番に実行されます。"))
        bl.addWidget(self.list_steps)
        cv.addWidget(box, 1)

        box = QtWidgets.QGroupBox("共通ポップアップ（順序を問わず割り込みを閉じる）")
        bl = QtWidgets.QVBoxLayout(box)
        self.list_popups = QtWidgets.QListWidget()
        self.list_popups.setIconSize(QtCore.QSize(64, 64))
        cv.addLayout(groupbox_help(
            "広告や「フレンド申請」など、どのステップの最中でも突然現れる"
            "可能性がある画面を登録する場所です。再生中はステップの実行順序に"
            "関係なく、これらの画像が見えたら優先して閉じてから元の操作を続けます。"))
        bl.addWidget(self.list_popups)
        cv.addWidget(box, 1)

        tabs.addTab(tab_content, "記録内容")

        # --- 失敗履歴タブ ---
        tab_fail = QtWidgets.QWidget()
        fv = QtWidgets.QVBoxLayout(tab_fail)

        btn_refresh_fail = QtWidgets.QPushButton("表示を更新（上のレシピ名を対象）")
        btn_refresh_fail.clicked.connect(self.refresh_history)
        fv.addWidget(with_help(
            btn_refresh_fail,
            "上のレシピ名で失敗の履歴を、この画面に読み込み直します。"))

        btn_clear_hist = QtWidgets.QPushButton("失敗履歴・ログを消去")
        btn_clear_hist.clicked.connect(self.on_clear_history)
        fv.addWidget(with_help(
            btn_clear_hist,
            "このレシピの失敗スクリーンショット・再生ログ・失敗履歴のみを"
            "削除します。記録したステップ画像や共通ポップアップは残ります。"))

        box = QtWidgets.QGroupBox("よく止まる箇所（失敗回数の多い順）")
        bl = QtWidgets.QVBoxLayout(box)
        self.tbl_rank = QtWidgets.QTableWidget(0, 2)
        self.tbl_rank.setHorizontalHeaderLabels(["ステップ", "失敗回数"])
        self.tbl_rank.horizontalHeader().setStretchLastSection(False)
        self.tbl_rank.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.Stretch)
        self.tbl_rank.verticalHeader().setVisible(False)
        self.tbl_rank.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tbl_rank.setMaximumHeight(120)
        bl.addWidget(self.tbl_rank)
        fv.addLayout(groupbox_help(
            "過去の再生でどのステップが何回失敗したかを、失敗回数の多い順に"
            "表示します。よく失敗するステップは、しきい値や切り抜き画像を"
            "見直す目安になります。"))
        fv.addWidget(box)

        box = QtWidgets.QGroupBox("失敗履歴（クリックでスクリーンショット表示）")
        bl = QtWidgets.QHBoxLayout(box)
        self.list_failures = QtWidgets.QListWidget()
        self.list_failures.itemClicked.connect(self.on_failure_selected)
        bl.addWidget(self.list_failures, 1)
        self.lbl_fail_preview = QtWidgets.QLabel("失敗履歴をクリックすると\nここに画像が表示されます")
        self.lbl_fail_preview.setAlignment(QtCore.Qt.AlignCenter)
        self.lbl_fail_preview.setMinimumSize(180, 180)
        self.lbl_fail_preview.setStyleSheet("background:#222; color:#aaa;")
        bl.addWidget(self.lbl_fail_preview, 1)
        fv.addLayout(groupbox_help(
            "過去に失敗した日時・ステップ・理由の一覧です。クリックすると、"
            "その時の端末画面のスクリーンショットを右側に表示します。"))
        fv.addWidget(box, 1)

        tabs.addTab(tab_fail, "失敗履歴")

        self.tabs = tabs
        tabs.currentChanged.connect(self.on_tab_changed)

        v.addWidget(tabs)

        # 停止・状態
        row = QtWidgets.QHBoxLayout()
        self.btn_stop = QtWidgets.QPushButton("停止")
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_stop.setEnabled(False)
        self.lbl_stat = QtWidgets.QLabel("未接続")
        row.addWidget(with_help(
            self.btn_stop,
            "実行中の記録・再生を止めます。再生中はキリの良いところで"
            "止まるまで少し時間がかかることがあります。"))
        row.addWidget(self.lbl_stat, 1)
        v.addLayout(row)

        # ログ
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        v.addWidget(self.log, 1)

    # ------------------------------------------------------------ 動作
    def append(self, msg):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{stamp}] {msg}")

    def closeEvent(self, event):
        """再生中に「停止」を押さずウィンドウを閉じても、PlayerThreadが
        動いたままアプリが終了して QThread のクラッシュにならないようにする"""
        if self.worker is not None and self.worker.isRunning():
            self.lbl_stat.setText("終了処理中…")
            self.append("終了処理中… 再生スレッドの停止を待っています")
            # setText直後はまだ画面に反映されていないため、
            # 後続のwait()でブロックする前に強制的に描画させる
            QtWidgets.QApplication.processEvents()
            self.worker.stop()
            finished = self.worker.wait(5000)
            if not finished:
                self.append(
                    "!! 再生スレッドが5秒以内に停止しませんでした。"
                    "終了処理を続行します")
        event.accept()

    def on_connect(self):
        self.serial = self.ed_serial.text().strip() or None
        try:
            d = core.connect(self.serial)
            info = d.device_info
            self.lbl_stat.setText(f"接続: {info.get('model')}  {d.window_size()}")
            self.append(f"接続成功: {info.get('model')} / {d.serial}")
        except Exception as e:
            self.lbl_stat.setText("接続失敗")
            self.append(f"!! 接続失敗: {e}")

    def _busy(self, busy):
        self.btn_rec.setEnabled(not busy)
        self.btn_play.setEnabled(not busy)
        self.btn_conn.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)

    def on_record(self):
        name = self.cmb_recipe.currentText().strip()
        if not name:
            self.append("!! レシピ名を入れてください")
            return
        if not is_valid_recipe_name(name):
            self.append(f"!! レシピ名に使えない文字が含まれています: {INVALID_NAME_CHARS}")
            return
        try:
            dlg = RecorderDialog(self.serial, name,
                                 self.sp_w.value(), self.sp_h.value(), self)
        except Exception as e:
            self.append(f"!! 記録の準備に失敗: {e}")
            return
        self.append(f"記録ウィンドウを開きました（{name}）")
        dlg.exec()
        self.append(f"記録ウィンドウを閉じました（{name}）")
        if self.cmb_recipe.findText(name) < 0:
            self.cmb_recipe.addItem(name)

    def on_play(self):
        name = self.cmb_recipe.currentText().strip()
        if not name:
            self.append("!! レシピ名を入れてください")
            return
        if not is_valid_recipe_name(name):
            self.append(f"!! レシピ名に使えない文字が含まれています: {INVALID_NAME_CHARS}")
            return
        self.worker = PlayerThread(
            self.serial, name,
            self.sp_loops.value(), self.sp_thr.value(),
            self.sp_to.value(), self.sp_after.value(),
            self.sp_poll.value(), 6, self.sp_fail.value(),
            verify=self.ck_verify.isChecked(), tap_retry=3,
            hold_ms=self.sp_hold.value()
        )
        self.worker.sig_log.connect(self.append)
        self.worker.sig_cycle.connect(
            lambda ok, ng: self.lbl_stat.setText(f"成功 {ok} / 失敗 {ng}")
        )
        self.worker.sig_done.connect(self.on_worker_done)
        self._busy(True)
        self.worker.start()

    def on_stop(self):
        if self.worker:
            self.worker.stop()
            self.append("停止要求を送りました…")

    def on_worker_done(self):
        self._busy(False)
        if self.cmb_recipe.findText(self.cmb_recipe.currentText()) < 0:
            self.cmb_recipe.addItem(self.cmb_recipe.currentText())
        self.refresh_history()

    def on_tab_changed(self, index):
        if self.tabs.tabText(index) in ("記録内容", "失敗履歴"):
            self.refresh_history()

    def on_clear_history(self):
        name = self.cmb_recipe.currentText().strip()
        if not name:
            self.append("!! レシピ名を入れてください")
            return
        if not is_valid_recipe_name(name):
            self.append(f"!! レシピ名に使えない文字が含まれています: {INVALID_NAME_CHARS}")
            return
        d = core.recipe_path(name)
        targets = (list(d.glob("error_*.png")) + list(d.glob("playback_*.log")) +
                   list(d.glob("failures.jsonl")))
        if not targets:
            self.append(f"「{name}」に消去する失敗履歴・ログはありません")
            return
        resp = QtWidgets.QMessageBox.question(
            self, "失敗履歴・ログを消去",
            f"「{name}」の失敗スクリーンショット・再生ログ・失敗履歴"
            f"（計{len(targets)}件）を削除します。\n"
            "記録したレシピ本体（ステップ画像）は削除されません。よろしいですか？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if resp != QtWidgets.QMessageBox.Yes:
            return
        removed = 0
        for f in targets:
            try:
                f.unlink()
                removed += 1
            except Exception as e:
                self.append(f"!! 削除失敗: {f.name} ({e})")
        self.append(f"「{name}」の失敗履歴・ログを{removed}件消去しました")
        self.refresh_history()

    def on_delete_recipe(self):
        name = self.cmb_recipe.currentText().strip()
        if not name:
            self.append("!! レシピ名を入れてください")
            return
        if not is_valid_recipe_name(name):
            self.append(f"!! レシピ名に使えない文字が含まれています: {INVALID_NAME_CHARS}")
            return
        if self.worker is not None and self.worker.isRunning() and self.worker.name == name:
            self.append(f"!! 「{name}」は再生中のため削除できません。先に停止してください")
            return
        d = core.recipe_path(name)
        if not (d / "recipe.json").exists():
            self.append(f"「{name}」はまだ記録されていません")
            return
        resp = QtWidgets.QMessageBox.question(
            self, "レシピを削除",
            f"「{name}」を完全に削除します。\n"
            "記録したステップ画像・共通ポップアップ・失敗履歴・ログもすべて削除され、"
            "元に戻せません。\n\n本当に削除しますか？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if resp != QtWidgets.QMessageBox.Yes:
            return
        try:
            shutil.rmtree(d)
        except Exception as e:
            self.append(f"!! 削除に失敗しました: {e}")
            return
        self.append(f"「{name}」を削除しました")
        idx = self.cmb_recipe.findText(name)
        if idx >= 0:
            self.cmb_recipe.removeItem(idx)
        self.cmb_recipe.setCurrentText("")
        self.refresh_history()

    def refresh_history(self):
        name = self.cmb_recipe.currentText().strip()
        self.list_steps.clear()
        self.list_popups.clear()
        self.tbl_rank.setRowCount(0)
        self.list_failures.clear()
        self.lbl_fail_preview.setPixmap(QtGui.QPixmap())
        self.lbl_fail_preview.setText("失敗履歴をクリックすると\nここに画像が表示されます")
        if not name:
            return

        d = core.recipe_path(name)
        recipe_file = d / "recipe.json"
        if recipe_file.exists():
            try:
                data = json.loads(recipe_file.read_text(encoding="utf-8"))
            except Exception as e:
                data = {"steps": []}
                self.append(f"!! レシピの読み込みに失敗: {e}")

            def add_thumb_item(list_widget, label, ctx_path):
                item = QtWidgets.QListWidgetItem(label)
                if ctx_path.exists():
                    # フルサイズ画像をそのままQIconにすると重いので縮小してから使う
                    pix = QtGui.QPixmap(str(ctx_path))
                    if not pix.isNull():
                        pix = pix.scaled(64, 64, QtCore.Qt.KeepAspectRatio,
                                          QtCore.Qt.SmoothTransformation)
                        item.setIcon(QtGui.QIcon(pix))
                list_widget.addItem(item)

            for i, s in enumerate(data.get("steps", []), 1):
                # "context"を優先し、無い(古い形式の)レシピはインデックスから
                # 組み立てる方式にフォールバックする。並び替え後はインデックスと
                # ファイル名の番号が一致しなくなるため、"context"が信頼できる
                ctx_name = s.get("context") or f"context_{i:02d}.png"
                add_thumb_item(self.list_steps, f"{i}. {s.get('label', '?')}",
                               d / ctx_name)
            for i, p in enumerate(data.get("popups", []), 1):
                ctx_name = p.get("context") or f"context_popup_{i:02d}.png"
                add_thumb_item(self.list_popups, f"P{i}. {p.get('label', '?')}",
                               d / ctx_name)
            if not data.get("popups"):
                self.list_popups.addItem("(共通ポップアップは未登録です)")
        else:
            self.list_steps.addItem("(このレシピはまだ記録されていません)")

        failures = core.load_failures(name)
        counts = {}
        for f in failures:
            label = f.get("step_label", "?")
            counts[label] = counts.get(label, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        self.tbl_rank.setRowCount(len(ranked))
        for row, (label, cnt) in enumerate(ranked):
            self.tbl_rank.setItem(row, 0, QtWidgets.QTableWidgetItem(label))
            self.tbl_rank.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{cnt}回"))

        for f in failures[:100]:
            text = f"{f.get('ts', '?')}  {f.get('step_label', '?')}  {f.get('reason', '')[:30]}"
            item = QtWidgets.QListWidgetItem(text)
            item.setData(QtCore.Qt.UserRole, f.get("screenshot"))
            self.list_failures.addItem(item)
        if not failures:
            self.list_failures.addItem("(失敗履歴はまだありません)")

    def on_failure_selected(self, item):
        fname = item.data(QtCore.Qt.UserRole)
        name = self.cmb_recipe.currentText().strip()
        if not fname or not name:
            return
        path = core.recipe_path(name) / fname
        if not path.exists():
            self.lbl_fail_preview.setText("画像が見つかりません")
            return
        pix = QtGui.QPixmap(str(path))
        pix = pix.scaledToHeight(240, QtCore.Qt.SmoothTransformation)
        self.lbl_fail_preview.setPixmap(pix)


if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    w = MainWindow()
    w.show()
    app.exec()
