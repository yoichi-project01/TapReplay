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


# マスク付きZNCC用: 連続撮影してマスクを作る際の設定値。
# 値を変えたい場合はここを調整する
MASK_CAPTURE_FRAMES = 6     # 撮影する枚数
MASK_CAPTURE_INTERVAL = 0.3  # 撮影間隔(秒)
MASK_STD_THRESHOLD = 8.0    # この標準偏差未満の画素だけを「動かない画素」としてマスクに含める
MASK_MIN_VALID_RATIO = 0.05  # マスクの有効画素率がこれ未満なら警告
# 撮影中に画面遷移してしまったこと(=別の画面を撮っていること)の検出しきい値。
# 隣接フレーム間の平均輝度差(0-255スケール)がこれを超えたら「大きく変化した」とみなす。
# クリック直後に画面遷移が始まるアプリでは、切り抜き範囲全体の色がガラッと
# 変わることが多く、単純な平均差分でも実用上十分検出できるため
MASK_TRANSITION_DIFF_THRESHOLD = 20.0

# ステップごとのしきい値の自動算出用(フェーズ3)。撮影した6枚それぞれに対して
# テンプレート＋マスクでマッチングし、その最小一致度からこの値を引いたものを
# 初期しきい値とする。下限はTHRESHOLD_AUTO_MINでクリップする
THRESHOLD_AUTO_MARGIN = 0.10
THRESHOLD_AUTO_MIN = 0.5


# ============================================ 記録ダイアログ（クリック式）
class RecorderDialog(QtWidgets.QDialog):
    def __init__(self, serial, name, tpl_w, tpl_h, parent=None, retake_label=None):
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
        # (キャンセルされた場合に元のレシピを壊さないため)。
        # 撮り直し(【撮り直し機能】)で差し替え前の古いファイルを消す際もこれに合流させる
        self._purge_on_save = None
        self._dirty = False  # 保存していない変更があるか(閉じる時の確認用)
        self.pil = None
        self.scale = 1.0
        self.shot_w, self.shot_h = None, None  # refresh()で最新のスクショサイズに更新される
        # 撮り直し対象として選ばれているステップ(dict、self.stepsの要素そのもの)。
        # Noneでなければ、次のon_clickは新規ステップ追加ではなく撮り直しとして扱う
        self._retake_step = None
        self._retake_seq = 0  # 撮り直しで書き出すファイル名の重複防止用連番

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

        b_retake = QtWidgets.QPushButton("選択したステップを撮り直す")
        b_retake.clicked.connect(self.on_retake_clicked)
        side.addWidget(with_help(
            b_retake,
            "一覧で選んだステップだけを、今の画面から撮り直して差し替えます。"
            "ラベルや実行順はそのまま維持されます。旧方式(ccoeff)で記録した"
            "既存レシピのうち、失敗しやすい一部のステップだけを新方式"
            "(マスク付きZNCC)へ移行したいときに使います。押した後、対象の"
            "ボタンが写るよう端末の画面を合わせてからクリックしてください。"))

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

        self._load_existing(auto_continue=(retake_label is not None))
        self._refresh_lists()
        self.refresh()
        if retake_label is not None:
            self._arm_retake_by_label(retake_label)

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

    def _load_existing(self, auto_continue=False):
        """同名レシピが既にあれば、続きから記録するか確認して読み込む。

        auto_continue=True(「失敗履歴」タブからの撮り直しショートカット起動時)
        の場合は、続きから記録する意図が呼び出し元で既に明確なので、
        確認ダイアログを出さず常に「続きから」を選んだものとして扱う"""
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

        if auto_continue:
            resp = QtWidgets.QMessageBox.Yes
        else:
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
                             "popup_*.png", "context_popup_*.png",
                             "mask_*.png", "mask_popup_*.png"):
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
        # 表示スケールはスクリーンショット自体のサイズを基準にする(window_size
        # ではない)。on_click()のrx,ryはこのスケールで逆算するため、ここを
        # window_size基準のままにすると、shot_size!=window_sizeの端末で
        # core.crop()に渡す座標(スクショ空間)とずれてしまう
        self.shot_w, self.shot_h = self.pil.size
        avail = QtWidgets.QApplication.primaryScreen().availableGeometry()
        maxh = int(avail.height() * 0.85)
        maxw = int(avail.width() * 0.6)
        self.scale = min(1.0, maxh / self.shot_h, maxw / self.shot_w)
        disp = self.pil.resize(
            (int(self.shot_w * self.scale), int(self.shot_h * self.scale)))
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

    def _capture_masked_template(self, rx, ry):
        """クリック位置(rx, ry)を中心に複数フレーム撮影し、マスク付きZNCC用の
        テンプレート・マスク・自動しきい値・確認用画像(ctx)を作る。

        新規ステップ/共通ポップアップの記録と、既存ステップの撮り直しの
        両方から呼ばれる共通処理(【撮り直し機能】追加にあたり on_click から
        切り出した)。戻り値: (tpl_gray, mask, dx, dy, computed_threshold, ctx)
        """
        # 端末にタップを送ると画面が進んでしまうため、この撮影は必ず
        # 「クリックを端末にも送る」の送信より前に行う
        self.img.setEnabled(False)
        self.setWindowTitle(f"記録: {self.name} (撮影中…)")
        self._msg("撮影中…(複数枚のスクリーンショットから動かない部分を抽出します)")
        QtWidgets.QApplication.processEvents()

        frames = [self.pil]  # 直近のrefresh()で撮った画面を1枚目として使う
        for _ in range(MASK_CAPTURE_FRAMES - 1):
            time.sleep(MASK_CAPTURE_INTERVAL)
            try:
                frames.append(self.d.screenshot())
            except Exception as e:
                self._msg(f"!! 撮影中にスクショ失敗: {e}")
                break
        self.setWindowTitle(f"記録: {self.name}")

        ctx = cv2.cvtColor(np.array(frames[0]), cv2.COLOR_RGB2BGR)
        cv2.rectangle(ctx,
                      (rx - self.tpl_w // 2, ry - self.tpl_h // 2),
                      (rx + self.tpl_w // 2, ry + self.tpl_h // 2),
                      (0, 0, 255), 4)

        dx = dy = 0
        crops_gray = []
        for f in frames:
            crop_img, (dx, dy) = core.crop(f, rx, ry, self.tpl_w, self.tpl_h)
            crops_gray.append(core.to_gray(crop_img).astype(np.float32))
        stack = np.stack(crops_gray)
        mask = (stack.std(axis=0) < MASK_STD_THRESHOLD).astype(np.uint8) * 255
        tpl_gray = stack.mean(axis=0).astype(np.uint8)
        valid_ratio = float((mask > 0).mean())

        # 撮影中に画面遷移してしまった(=別の画面を撮ってしまった)ことの簡易検出。
        # ページ遷移中は切り抜き範囲に限らず画面全体の絵が入れ替わるため、
        # 隣接フレーム間で画面全体の平均輝度が大きく動くことが多い。これを
        # 目安に「遷移中に撮ってしまった疑いがある」ことを検出する
        full_grays = [core.to_gray(f).astype(np.float32) for f in frames]
        transitioned = any(
            abs(float(full_grays[i].mean()) - float(full_grays[i - 1].mean()))
            > MASK_TRANSITION_DIFF_THRESHOLD
            for i in range(1, len(full_grays))
        )

        # ステップごとのしきい値を自動算出する。撮影した各フレームに対して
        # 作ったテンプレート＋マスクでマッチングし、その最小一致度から
        # マージン分を引いたものを初期値にする(=どのフレームでも確実に
        # 拾えるよう、実際に観測した中で最も弱いスコアを基準にする)
        scores = [core.match(g, tpl_gray, 0.0, method="masked_zncc", mask=mask)[2]
                  for g in full_grays]
        min_score = min(scores)
        raw_threshold = min_score - THRESHOLD_AUTO_MARGIN
        computed_threshold = max(THRESHOLD_AUTO_MIN, raw_threshold)
        clip_note = (f"(下限{THRESHOLD_AUTO_MIN:.2f}でクリップ)"
                     if computed_threshold > raw_threshold else "")
        self._msg(
            f"  しきい値を自動算出: 最小一致度{min_score:.3f} - "
            f"{THRESHOLD_AUTO_MARGIN:.2f} = {computed_threshold:.3f} {clip_note}".rstrip())

        if valid_ratio < MASK_MIN_VALID_RATIO:
            self._msg(
                f"  !! 注意: 動かない部分がほとんどありません(有効画素率"
                f"{valid_ratio * 100:.1f}%)。暗転・読み込み画面などに誤反応しやすい"
                "ので、文字や模様が入るよう「切抜き幅／高さ」を広げるか、"
                "別の場所をクリックし直すことをおすすめします")
        if transitioned:
            self._msg(
                "  !! 注意: 撮影中(約2秒)に画面が大きく変化しました。画面遷移の"
                "途中で撮影してしまった可能性があるので、少し待ってから同じ場所を"
                "撮り直すことをおすすめします")

        return tpl_gray, mask, dx, dy, computed_threshold, ctx

    def _dispatch_click_tap_and_refresh(self, rx, ry):
        """「クリックを端末にも送る」がONならタップを送って画面更新を待ち、
        OFFならすぐ画面を更新する(新規記録・撮り直し共通の末尾処理)。

        rx, ryはスクリーンショット空間の座標。adb shell input tapは表示解像度
        (window_size)で解釈されるため、端末へ送る直前にだけ変換する"""
        if self.ck_send.isChecked():
            try:
                tx, ty = core.shot_to_window(
                    rx, ry, self.shot_w, self.shot_h, self.sw, self.sh)
                core.tap(self.serial or self.d.serial, tx, ty)
                self._msg("  端末にタップ送信 → 画面更新までクリック無効…")
            except Exception as e:
                self._msg(f"  !! タップ送信失敗: {e}")
            # 更新が終わるまで誤クリック（古い画面での記録）を防ぐ
            self.img.setEnabled(False)
            QtCore.QTimer.singleShot(
                int(self.sp_delay.value() * 1000), self.refresh)
        else:
            self.refresh()

    def on_click(self, lx, ly):
        if self.pil is None:
            return
        self._dirty = True
        rx = int(lx / self.scale)
        ry = int(ly / self.scale)

        if self._retake_step is not None:
            self._do_retake(rx, ry)
            return

        tpl_gray, mask, dx, dy, computed_threshold, ctx = \
            self._capture_masked_template(rx, ry)

        if self.ck_popup_mode.isChecked():
            idx = len(self.popups) + 1
            tpl = f"popup_{idx:02d}.png"
            ctx_name = f"context_popup_{idx:02d}.png"
            mask_name = f"mask_popup_{idx:02d}.png"
            core.imwrite(self.dir / tpl, tpl_gray)
            core.imwrite(self.dir / mask_name, mask)
            core.imwrite(self.dir / ctx_name, ctx)
            new_item = {"label": f"ポップアップ{idx}", "template": tpl,
                        "context": ctx_name, "x": rx, "y": ry, "dx": dx, "dy": dy,
                        "method": "masked_zncc", "mask": mask_name,
                        "threshold": computed_threshold}
            self.popups.append(new_item)
            self._history.append(("popup", new_item))
            self._msg(f"popup{idx}: ({rx},{ry}) → {tpl} (共通ポップアップとして記録)")
        else:
            idx = len(self.steps) + 1
            tpl = f"step_{idx:02d}.png"
            ctx_name = f"context_{idx:02d}.png"
            mask_name = f"mask_{idx:02d}.png"
            core.imwrite(self.dir / tpl, tpl_gray)
            core.imwrite(self.dir / mask_name, mask)
            core.imwrite(self.dir / ctx_name, ctx)
            new_item = {"label": f"タップ{idx}", "template": tpl,
                        "context": ctx_name, "x": rx, "y": ry, "dx": dx, "dy": dy,
                        "method": "masked_zncc", "mask": mask_name,
                        "threshold": computed_threshold}
            self.steps.append(new_item)
            self._history.append(("step", new_item))
            self._msg(f"step{idx}: ({rx},{ry}) → {tpl}")
        self._refresh_lists()
        self._dispatch_click_tap_and_refresh(rx, ry)

    def _find_step_index(self, step_obj):
        """self.steps内でstep_objと同一のオブジェクト(is)を探し、そのインデックスを
        返す。見つからなければ-1(並び替え・一つ戻す等で既に無くなっている場合)"""
        for i, s in enumerate(self.steps):
            if s is step_obj:
                return i
        return -1

    def _arm_retake(self, idx):
        """self.steps[idx]を撮り直し対象として選ぶ。次のon_clickで差し替えが
        実行される"""
        if not (0 <= idx < len(self.steps)):
            return
        if self._retake_step is not None:
            self._msg(
                "!! 既に撮り直し待ちのステップがあります。先にそのボタンを"
                "クリックして撮影を完了するか、一覧から選び直してください")
            return
        self._retake_step = self.steps[idx]
        label = self._retake_step["label"]
        self.setWindowTitle(
            f"記録: {self.name} ─ 「{label}」を撮り直し中(対象をクリック)")
        self._msg(
            f"「{label}」を撮り直します。対象のボタンが写るよう端末の画面を"
            "合わせてから、そのボタンをクリックしてください"
            "(新方式masked_znccで差し替えられます。順番・名前は維持されます)")

    def _arm_retake_by_label(self, label):
        """指定ラベルのステップを撮り直しモードにする(「失敗履歴」タブからの
        ショートカット起動用)。見つからない場合(名前変更・削除等)は、
        通常の記録ダイアログとして開いたままにし、その旨だけログに出す"""
        for i, s in enumerate(self.steps):
            if s.get("label") == label:
                self.list_steps_edit.setCurrentRow(i)
                self._arm_retake(i)
                return
        self._msg(
            f"!! 「{label}」という名前のステップが見つかりませんでした"
            "(名前が変更された可能性があります)。一覧から選び直してください")

    def on_retake_clicked(self):
        idx = self.list_steps_edit.currentRow()
        if not (0 <= idx < len(self.steps)):
            self._msg("!! 撮り直すステップを一覧から選択してください")
            return
        self._arm_retake(idx)

    def _do_retake(self, rx, ry):
        step = self._retake_step
        self._retake_step = None
        idx = self._find_step_index(step)
        if idx < 0:
            self._msg(
                "!! 撮り直し対象のステップが見つかりません"
                "(一つ戻す・並び替え等で変わった可能性があります)")
            self.setWindowTitle(f"記録: {self.name}")
            self.refresh()
            return

        tpl_gray, mask, dx, dy, computed_threshold, ctx = \
            self._capture_masked_template(rx, ry)

        # 差し替え前の古いファイルは、保存されるまで削除しない
        # (「最初からやり直す」の_purge_on_saveと同じ考え方に合流させる。
        # 新しいファイルは別名で書き出すので、ここではまだ何も消さない)
        if self._purge_on_save is None:
            self._purge_on_save = set()
        for fname in (step.get("template"), step.get("context"), step.get("mask")):
            if fname:
                self._purge_on_save.add(fname)

        self._retake_seq += 1
        tpl = f"step_{idx + 1:02d}_retake{self._retake_seq}.png"
        ctx_name = f"context_{idx + 1:02d}_retake{self._retake_seq}.png"
        mask_name = f"mask_{idx + 1:02d}_retake{self._retake_seq}.png"
        core.imwrite(self.dir / tpl, tpl_gray)
        core.imwrite(self.dir / mask_name, mask)
        core.imwrite(self.dir / ctx_name, ctx)

        # ラベル・実行順(リスト内の位置)は維持したまま、中身だけ差し替える
        step["template"] = tpl
        step["context"] = ctx_name
        step["mask"] = mask_name
        step["method"] = "masked_zncc"
        step["threshold"] = computed_threshold
        step["x"] = rx
        step["y"] = ry
        step["dx"] = dx
        step["dy"] = dy

        self._msg(f"「{step['label']}」を撮り直しました(新方式masked_znccに切替)")
        self._refresh_lists()
        self._dispatch_click_tap_and_refresh(rx, ry)

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
        for fname in (obj.get("template"), obj.get("context"), obj.get("mask")):
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
                keep.add(item.get("mask"))
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
            # スクショ空間で記録した座標(x/y/dx/dy)を再生側が正しく解釈できる
            # ように、記録時のスクリーンショット解像度も保存しておく
            "screenshot_size": [self.shot_w, self.shot_h],
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

    # 共通ポップアップの探索を間引く最小間隔(秒)。実測(1920x1080画面 x
    # 200x100テンプレ)でmasked_zncc 1回は約137ms(ccoeffの約2.6倍)かかり、
    # ポップアップは登録されている全件を毎ポーリング走査するため、件数が
    # 増えると1回の確認だけで数百ms〜1秒近くに膨らみ、既定のポーリング間隔
    # (1.5秒)を圧迫する。本命ステップの検出は毎回そのまま行いつつ、
    # ポップアップ側の探索頻度だけをこの間隔に落とすことで、検出の
    # 反応速度をあまり犠牲にせずに総コストを下げる。
    # (ROIによる探索範囲の絞り込みも検討したが、dx/dyのずれや画面回転などで
    # 記録位置と実際の出現位置がずれるケースの考慮・検証が増えて複雑になる
    # ため、まずは副作用の少ない間引きを採用した)
    POPUP_CHECK_INTERVAL = 1.0

    # window_size空間への変換倍率(横×scale_x, 縦×scale_y)の相対差がこれを
    # 超えたら「アスペクト比が違う」として警告する。ステータスバー分の数px
    # の差やDPI丸めなど、良性の誤差は数%程度に収まることが多いのに対し、
    # 縦横比が崩れる典型例(記録時と再生時で画面の向きが違う、解像度設定を
    # 上書きした等)はscale_xとscale_yが数十%〜数倍単位で乖離するため、
    # 誤検知と見逃しのバランスを見て15%に設定した
    ASPECT_MISMATCH_THRESHOLD = 0.15

    def __init__(self, serial, name, loops, threshold_offset,
                 step_timeout, after, poll, jitter, max_fail,
                 verify=True, tap_retry=3, hold_ms=0):
        super().__init__()
        self.serial = serial
        self.name = name
        self.loops = loops
        self.threshold_offset = threshold_offset
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
        self._last_popup_check = 0.0
        # 直近の失敗で「どの手法がどれだけ迫っていたか」を保持しておき、
        # failures.jsonlへの記録(【実装3】)に使う
        self._last_attempts = None
        # 表示解像度(adb inputが解釈する座標系)。run()の冒頭でd.window_size()
        # から設定される。タップ直前のスクショ空間→表示解像度変換に使う
        self.sw = None
        self.sh = None

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

    def _to_window(self, x, y, shot_shape):
        """スクリーンショット空間の座標(x, y)を、実際にタップを送る直前に
        表示解像度(self.sw, self.sh)空間へ変換する。shot_shapeはマッチング
        に使ったグレースケール画像のshape((h, w))で、呼び出しごとの
        スクリーンショットから直接求める(向き変更等にも追従できるように、
        run()開始時の値を使い回さない)"""
        shot_h, shot_w = shot_shape[:2]
        return core.shot_to_window(x, y, shot_w, shot_h, self.sw, self.sh)

    def _effective_threshold(self, step):
        """そのステップで実際に使う一致しきい値を返す。

        "threshold"キー(フェーズ3以降に記録したステップが持つ、記録時に
        自動算出された基準値)があれば、それにGUIの調整値(offset)を足した
        ものを使う。"threshold"キーを持たない(フェーズ2以前の)既存レシピの
        ステップは、ステップごとの基準値が存在しないため、後方互換として
        従来通りGUIの値をそのまま一致しきい値として使う"""
        base = step.get("threshold")
        if base is None:
            return self.threshold_offset
        return base + self.threshold_offset

    def _effective_threshold_edge(self, step):
        """エッジフォールバック(method="edge")用のしきい値を返す。

        エッジのスコアはmasked_zncc/ccoeffとは尺度が全く異なる(目安は
        0.45前後 vs 0.85前後)ため、ステップの"threshold"を使い回さない。
        記録時に専用の"threshold_edge"を実測して保存する案もあったが、
        エッジ検出はあくまでmasked_zncc失敗時だけの補助的なフォールバック
        であり、記録のたびに追加でマッチングして精密なしきい値を実測する
        コストに見合わないと判断し、手法ごとの固定値(core.DEFAULT_THRESHOLDS)
        を使うことにした。GUIの全体オフセット(±0.2)もmasked_znccの尺度に
        合わせたものなので、ここには適用しない(スケールが違いすぎて
        意味がずれるため)"""
        return core.DEFAULT_THRESHOLDS["edge"]

    def _match_candidate(self, gray, cand):
        """candをgray画面に対してマッチングする(【実装1】【実装2】)。

        candの"method"(無ければ"ccoeff")に応じてcore.match()を呼び分ける。
        method="masked_zncc"のステップが見つからなかった場合に限り、同じ
        画面に対してmethod="edge"でもフォールバック探索する。method="ccoeff"
        (旧レシピ)ではフォールバックしない(既存レシピの挙動を変えないため)。

        戻り値: (cx, cy, val, method_used, thr_used, attempts)
            - 見つかった場合: cx/cyはタップ位置、method_usedは実際に
              検出できた手法("edge"ならフォールバックで見つかったことを示す)
            - 見つからなかった場合: cx=cy=None。method_used/thr_usedは
              主手法(candの"method")のもの
            - attempts: [(method, val, threshold), ...] 試した手法すべての
              記録(ログ・failures.jsonl用)
        """
        method = cand.get("method", "ccoeff")
        thr = self._effective_threshold(cand)
        mask = cand.get("_mask") if method == "masked_zncc" else None
        cx, cy, val = core.match(gray, cand["_gray"], thr, method=method, mask=mask)
        attempts = [(method, val, thr)]
        if cx is not None:
            return cx, cy, val, method, thr, attempts
        if method == "masked_zncc":
            edge_thr = self._effective_threshold_edge(cand)
            ecx, ecy, eval_ = core.match(gray, cand["_gray"], edge_thr, method="edge")
            attempts.append(("edge", eval_, edge_thr))
            if ecx is not None:
                return ecx, ecy, eval_, "edge", edge_thr, attempts
        return None, None, val, method, thr, attempts

    def _attempts_summary(self, best_by_method):
        """{手法名: (最高一致度, しきい値)} を failures.jsonl 用のJSON化しやすい
        リスト形式に変換する(【実装3】)"""
        return [{"method": m, "score": round(float(v), 3), "threshold": round(float(t), 3)}
                for m, (v, t) in best_by_method.items()]

    def _find_best_match(self, gray, candidates):
        """candidates のうち今の画面に写っているものを探す(一致度が最も高いものを返す)。
        戻り値は (candidates内でのインデックス, 候補dict, cx, cy, val,
        method_used, thr_used, attempts) または見つからなければ None。
        ほぼ無地のテンプレートは暗転画面などに誤検知しやすいため対象から除外する"""
        best = None
        for i, s in enumerate(candidates):
            if not core.is_distinctive(s["_gray"]):
                continue
            cx, cy, val, method_used, thr_used, attempts = self._match_candidate(gray, s)
            if cx is not None and (best is None or val > best[4]):
                best = (i, s, cx, cy, val, method_used, thr_used, attempts)
        return best

    def _dismiss_popup_if_any(self, gray, popups):
        """共通ポップアップ(広告・フレンド申請等)が写っていれば閉じる。閉じたらTrue"""
        import random
        hit = self._find_best_match(gray, popups)
        if hit is None:
            return False
        _, popup, pcx, pcy, pval, pmethod, pthr, pattempts = hit
        jx = pcx + popup.get("dx", 0) + random.randint(-self.jitter, self.jitter)
        jy = pcy + popup.get("dy", 0) + random.randint(-self.jitter, self.jitter)
        tx, ty = self._to_window(jx, jy, gray.shape)
        core.tap(self._serial, tx, ty, self.hold_ms)
        fallback_note = "(エッジ判定で検出)" if pmethod == "edge" else ""
        self._log(
            f"    !! 共通ポップアップ「{popup['label']}」を検知"
            f"(一致{pval:.2f}[{pmethod}]){fallback_note}したので閉じました"
            f" (タップ{tx},{ty})")
        time.sleep(self.after)
        return True

    def _maybe_dismiss_popup(self, gray, popups):
        """待機ループ中の共通ポップアップ探索を間引いて呼ぶ(POPUP_CHECK_INTERVAL秒に1回)。
        タップ直後の「効いたか確認」時は_dismiss_popup_if_anyを直接呼ぶこと
        (頻度が低くタップのたびなので間引く必要が薄く、割り込み検知の
        取りこぼしを避けたいため)"""
        now = time.time()
        if now - self._last_popup_check < self.POPUP_CHECK_INTERVAL:
            return False
        self._last_popup_check = now
        return self._dismiss_popup_if_any(gray, popups)

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
        # best_by_method: {手法名: (これまでの最高一致度, その時のしきい値)}。
        # 試した手法すべての最高値を残しておき、タイムアウト/失敗時の
        # ヒント表示とfailures.jsonlへの記録(【実装3】)に使う
        best_by_method = {}
        self._last_attempts = None
        last_report = time.time()
        while time.time() < deadline:
            if self._stop:
                raise KeyboardInterrupt
            gray = core.to_gray(d.screenshot())

            # 共通ポップアップは、対象ステップの探索より先にチェックする
            # (どのステップを待っていても、順序に関係なく割り込んで閉じる)。
            # ただしmasked_znccは重いため、間引いて探索する(POPUP_CHECK_INTERVAL)
            if self._maybe_dismiss_popup(gray, popups):
                continue

            cx, cy, val, method_used, thr_used, attempts = self._match_candidate(gray, step)
            for m, v, t in attempts:
                cur = best_by_method.get(m)
                if cur is None or v > cur[0]:
                    best_by_method[m] = (v, t)
            self._last_attempts = self._attempts_summary(best_by_method)

            if cx is not None:
                # 見つかった → タップ。効かなければ押し直す。
                # 成功判定は「押したボタンが画面から消えたか」で見る
                # （背景アニメに惑わされない）
                fallback_note = "(エッジ判定で検出)" if method_used == "edge" else ""
                popup_interrupted = False
                cur_shape = gray.shape  # cx,cyがどのスクショ上の座標かを覚えておく
                for attempt in range(1, self.tap_retry + 1):
                    jx = cx + step.get("dx", 0) + random.randint(-self.jitter, self.jitter)
                    jy = cy + step.get("dy", 0) + random.randint(-self.jitter, self.jitter)
                    tx, ty = self._to_window(jx, jy, cur_shape)
                    core.tap(self._serial, tx, ty, self.hold_ms)
                    tag = f" [{attempt}回目]" if attempt > 1 else ""
                    self._log(
                        f"    {step['label']}: タップ({tx},{ty}) "
                        f"一致{val:.2f}[{method_used}]{fallback_note}{tag}")
                    time.sleep(self.after)
                    if not self.verify:
                        return idx
                    after_gray = core.to_gray(d.screenshot())
                    # ボタンが消えずに残っているように見えても、実は共通ポップアップに
                    # 覆われていて反応していないだけ、というケースがあるため先に確認する
                    # (タップ直後の確認は頻度が低いため間引かず毎回チェックする)
                    if self._dismiss_popup_if_any(after_gray, popups):
                        popup_interrupted = True
                        break
                    ncx, ncy, nval, nmethod, nthr, nattempts = self._match_candidate(
                        after_gray, step)
                    for m, v, t in nattempts:
                        cur = best_by_method.get(m)
                        if cur is None or v > cur[0]:
                            best_by_method[m] = (v, t)
                    if ncx is None:
                        return idx  # ボタンが消えた＝タップ成功、次へ
                    # まだ同じボタンが見えている＝タップが効いていない → 押し直す
                    cx, cy, method_used = ncx, ncy, nmethod
                    cur_shape = after_gray.shape
                    fallback_note = "(エッジ判定で検出)" if method_used == "edge" else ""
                    self._log(
                        f"    …まだボタンが残っています(一致{nval:.2f}[{nmethod}])。押し直します")
                self._last_attempts = self._attempts_summary(best_by_method)
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
                local_idx, other_step, ocx, ocy, oval, omethod, othr, oattempts = other
                target_idx = idx + 1 + local_idx
                jx = ocx + other_step.get("dx", 0) + random.randint(-self.jitter, self.jitter)
                jy = ocy + other_step.get("dy", 0) + random.randint(-self.jitter, self.jitter)
                tx, ty = self._to_window(jx, jy, gray.shape)
                core.tap(self._serial, tx, ty, self.hold_ms)
                fallback_note = "(エッジ判定で検出)" if omethod == "edge" else ""
                self._log(
                    f"    !! ステップ{idx + 1}「{step['label']}」をスキップして"
                    f"ステップ{target_idx + 1}「{other_step['label']}」へ進みました"
                    f"(この先の画像を検知・一致{oval:.2f}[{omethod}]){fallback_note}")
                time.sleep(self.after)
                # 元の対象を待ち続けても二度と現れないので、進んだ先から再開する
                return target_idx

            # まだ見つからない → 数秒おきに現在の一致度を報告
            if time.time() - last_report >= 3:
                last_report = time.time()
                detail = " / ".join(
                    f"{m}:{v:.2f}(しきい値{t:.2f})" for m, (v, t) in best_by_method.items())
                self._log(f"    待機中… {step['label']} 最高一致度 {detail}")
            time.sleep(self.poll)

        # タイムアウト → 最高一致度から原因を推定してヒントを出す
        primary_method = step.get("method", "ccoeff")
        primary_val, primary_thr = best_by_method.get(primary_method, (0.0, 0.0))
        if primary_val >= primary_thr - 0.05:
            hint = "→ ほぼ一致。しきい値を少し下げれば拾えそう"
        elif primary_val >= 0.6:
            hint = "→ 惜しい。切抜きを見直すか、しきい値を下げる"
        else:
            hint = "→ この画面に対象が無い。前のタップが効いていない可能性大"
        detail = " / ".join(
            f"{m}:{v:.2f}(しきい値{t:.2f})" for m, (v, t) in best_by_method.items())
        raise TimeoutError(f"{step['label']} が出現せず ({detail}) {hint}")

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
            self.sw, self.sh = d.window_size()
            if data.get("device_size") and list(data["device_size"]) != [self.sw, self.sh]:
                self._log(
                    f"!! 注意: 記録時({data['device_size']})と画面サイズが違います。"
                    "解像度・向きを合わせてください"
                )

            # 内部の座標(マッチング結果・dx/dy)はスクリーンショット空間で
            # 統一しており、タップ直前にだけ表示解像度(self.sw, self.sh)へ
            # 変換する(_to_window)。ここでは、その変換が信頼できる状況か
            # どうかを再生開始前に確認しておく
            try:
                shot_w, shot_h = d.screenshot().size
            except Exception as e:
                shot_w = shot_h = None
                self._log(f"!! 注意: 座標系確認用のスクリーンショット取得に失敗しました: {e}")

            recorded_shot_size = data.get("screenshot_size")
            if shot_w is not None:
                if recorded_shot_size is not None:
                    # 新形式: 記録時のスクショ解像度が分かっているので、
                    # 今の解像度と直接比較できる
                    if list(recorded_shot_size) != [shot_w, shot_h]:
                        self._log(
                            f"!! 注意: 記録時のスクリーンショット解像度"
                            f"{tuple(recorded_shot_size)}と今の解像度"
                            f"({shot_w},{shot_h})が違います。タップ位置が"
                            "ずれる可能性があります")
                else:
                    # 旧形式("screenshot_size"を持たない): 記録時のスクショ
                    # 解像度が分からないため直接比較はできない(スキップ)。
                    # ただし旧形式のx/y/dx/dyは「window_size空間で記録した
                    # つもりで、実際はスクショをwindow_size座標で切った」
                    # 中途半端な値のため、記録時にスクショ解像度とwindow_size
                    # が一致していた場合のみ正しく動く。それを後から確かめる
                    # 手段はないが、今のこの端末で両者が一致していなければ、
                    # 記録時も同様の食い違いだった可能性が高いとみなし、
                    # 自動修復はせず再記録を促すだけに留める(挙動は変えない
                    # ―― 一致していれば当時と同じ計算になり従来通り動く)
                    if (shot_w, shot_h) != (self.sw, self.sh):
                        self._log(
                            "!! 注意: 座標系の情報を持たない旧形式のレシピで、"
                            "かつ今の端末はスクリーンショット解像度と画面解像度が"
                            "一致していません。このレシピは記録し直しが必要な"
                            "可能性があります")

                # 表示解像度への変換倍率(縦横別)が大きく異なる場合、スクショと
                # 画面のアスペクト比が違う(向きの不一致など)ため、変換式
                # そのものが信頼できない。旧形式・新形式を問わず今の端末の
                # 状態そのものについての警告なので、常にチェックする
                scale_x = self.sw / shot_w
                scale_y = self.sh / shot_h
                rel_diff = abs(scale_x - scale_y) / max(scale_x, scale_y)
                if rel_diff > self.ASPECT_MISMATCH_THRESHOLD:
                    self._log(
                        f"!! 注意: 画面の縦横で表示解像度への倍率が大きく違います"
                        f"(横×{scale_x:.2f} / 縦×{scale_y:.2f})。スクリーンショットと"
                        "画面解像度のアスペクト比が違う(向きの不一致など)可能性が"
                        "あり、タップ位置が信用できません")

            popups = data.get("popups", [])
            # ステップごとのしきい値("threshold"キー)を持たない旧形式のレシピが
            # 1件でもあれば、GUIの調整欄の値域・初期値が変わっていることに
            # よるユーザーの混乱を避けるため、その旨を1行ログに出しておく
            if any("threshold" not in s for s in data["steps"] + popups):
                self._log(
                    "!! 注意: ステップごとのしきい値を持たない旧形式のレシピです。"
                    f"「一致しきい値の調整（全体）」欄の値({self.threshold_offset:.2f})"
                    "がそのまま一致しきい値として使われます。この欄は値域・初期値が"
                    "変わっているので、検出しない/誤検出する場合は値を調整してください")
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
                        core.append_failure(self.name, step_label, str(e), fname,
                                             attempts=self._last_attempts)
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
        self.sp_thr_offset = QtWidgets.QDoubleSpinBox()
        self.sp_thr_offset.setRange(-0.2, 0.2)
        self.sp_thr_offset.setSingleStep(0.01)
        self.sp_thr_offset.setValue(0.0)
        self.sp_to = QtWidgets.QSpinBox(); self.sp_to.setRange(5, 3600); self.sp_to.setValue(300)
        self.sp_after = QtWidgets.QDoubleSpinBox(); self.sp_after.setRange(0, 20); self.sp_after.setValue(1.2)
        self.sp_poll = QtWidgets.QDoubleSpinBox(); self.sp_poll.setRange(0.3, 10); self.sp_poll.setValue(1.5)
        self.sp_fail = QtWidgets.QSpinBox(); self.sp_fail.setRange(1, 50); self.sp_fail.setValue(3)
        self.sp_hold = QtWidgets.QSpinBox(); self.sp_hold.setRange(0, 1000); self.sp_hold.setValue(0)
        self.sp_jitter = QtWidgets.QSpinBox(); self.sp_jitter.setRange(0, 100); self.sp_jitter.setValue(6)
        g.addWidget(help_label(
            "実行回数(0=無限)",
            "再生を何回繰り返すかを指定します。0にすると「停止」を押すまで"
            "無限に繰り返します。"), 0, 0)
        g.addWidget(self.sp_loops, 0, 1)
        g.addWidget(help_label(
            "一致しきい値の調整（全体）",
            "各ステップのしきい値は記録時に自動算出されており、実際に使う"
            "しきい値は「そのステップの基準値＋この調整値」です(-0.2〜+0.2)。"
            "ボタンが見つからない場合はマイナス方向(-0.05, -0.10 など)に、"
            "逆に別の場所を誤って検出してしまう場合はプラス方向に動かして"
            "ください。ログに出る「しきい値」「最高一致度」が目安になります。"
            "\n※ステップごとのしきい値を持たない古い形式のレシピでは、"
            "この値がそのまま一致しきい値として使われます。"), 0, 2)
        g.addWidget(self.sp_thr_offset, 0, 3)
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
        g.addWidget(help_label(
            "タップ位置のばらつきpx",
            "タップする座標を毎回この範囲内でランダムにずらす量(ピクセル)。"
            "0にすると常に全く同じ座標をタップします。同じ場所ばかり連打する"
            "ことで一部のアプリの不正操作対策に引っかかるのを避けるためのもので、"
            "通常は初期値のままで問題ありません。"), 4, 0)
        g.addWidget(self.sp_jitter, 4, 1)
        self.btn_play = QtWidgets.QPushButton("再生開始")
        self.btn_play.clicked.connect(self.on_play)
        g.addWidget(with_help(
            self.btn_play, "上で選んだレシピを、この設定で再生します。"), 5, 0, 1, 4)
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
        btn_retake_rank = QtWidgets.QPushButton("選択した項目を撮り直す(記録画面を開く)")
        btn_retake_rank.clicked.connect(self.on_retake_from_rank)
        bl.addWidget(with_help(
            btn_retake_rank,
            "選んだステップだけを撮り直すために、記録画面を開きます"
            "(要: 端末接続)。上の「レシピ名」欄のレシピを続きから記録する"
            "状態で開き、対象のステップが自動で撮り直し待ちになります。"
            "端末に対象の画面を出してから、そのボタンをクリックしてください。"))
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

    def on_record(self, *, retake_label=None):
        name = self.cmb_recipe.currentText().strip()
        if not name:
            self.append("!! レシピ名を入れてください")
            return
        if not is_valid_recipe_name(name):
            self.append(f"!! レシピ名に使えない文字が含まれています: {INVALID_NAME_CHARS}")
            return
        try:
            dlg = RecorderDialog(self.serial, name,
                                 self.sp_w.value(), self.sp_h.value(), self,
                                 retake_label=retake_label)
        except Exception as e:
            self.append(f"!! 記録の準備に失敗: {e}")
            return
        self.append(f"記録ウィンドウを開きました（{name}）")
        dlg.exec()
        self.append(f"記録ウィンドウを閉じました（{name}）")
        if self.cmb_recipe.findText(name) < 0:
            self.cmb_recipe.addItem(name)

    def on_retake_from_rank(self):
        """「よく止まる箇所」ランキングで選んだ行のステップを、記録画面を
        開いて直接撮り直しへ進める(【併せて】のショートカット)"""
        row = self.tbl_rank.currentRow()
        if row < 0:
            self.append("!! 「よく止まる箇所」の一覧から撮り直したいステップを選択してください")
            return
        item = self.tbl_rank.item(row, 0)
        label = item.text() if item else ""
        if not label:
            return
        self.on_record(retake_label=label)

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
            self.sp_loops.value(), self.sp_thr_offset.value(),
            self.sp_to.value(), self.sp_after.value(),
            self.sp_poll.value(), self.sp_jitter.value(), self.sp_fail.value(),
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
            # attempts: そのステップの検出で試した手法ごとの最高一致度
            # (【実装3】)。どの手法が実際に効いているかを一覧で分かるようにする
            attempts = f.get("attempts")
            if attempts:
                summary = " / ".join(
                    f"{a.get('method', '?')}:{a.get('score', 0):.2f}" for a in attempts)
                text += f"  [{summary}]"
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
