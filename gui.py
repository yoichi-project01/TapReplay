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
import re
import json
import time
import shutil
import pathlib
import datetime

import cv2
import numpy as np
from PySide6 import QtWidgets, QtCore, QtGui

import core
import notify

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


def format_duration(seconds):
    """秒数を「1時間23分」「4分5秒」「12秒」のような読みやすい表記にする
    (進捗表示の経過時間・1周あたりの平均時間に使う)"""
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}時間{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def make_collapsible_box(title, checked=False):
    """折りたたみ可能なQGroupBoxを作る。QGroupBox.setCheckable(True)の
    チェック状態を「開いているか(中身を表示しているか)」として使う、
    既存のQt部品だけで実現する簡単な方法。日常的には触らない詳細設定を
    既定で畳んでおき、必要なときだけ開けるようにするために使う。

    戻り値は (box, 中身を配置するQGridLayout)。boxをそのまま親レイアウトへ
    addWidgetし、中身は戻り値のgridへaddWidget(w, row, col)で追加すること"""
    box = QtWidgets.QGroupBox(title)
    box.setCheckable(True)
    box.setChecked(checked)
    inner = QtWidgets.QWidget()
    grid = QtWidgets.QGridLayout(inner)
    outer = QtWidgets.QVBoxLayout(box)
    outer.setContentsMargins(0, 4, 0, 0)
    outer.addWidget(inner)
    inner.setVisible(checked)
    box.toggled.connect(inner.setVisible)
    return box, grid


# ============================================ クリックできる画像ラベル
class ClickableLabel(QtWidgets.QLabel):
    """画面プレビュー上のクリック/ドラッグを検出する。

    その場でほぼ動かさずに離した場合はクリック(clicked)として扱い、従来
    通り1点だけを渡す(切抜き範囲は呼び出し側の既定値=「切抜き幅/高さ」を
    使う)。一定以上動かして離した場合はドラッグ(dragged)として扱い、
    矩形の対角2点を渡す。

    ドラッグ対応の理由: 既定サイズの自動切り抜きだと、アニメーション
    (光の演出など)がかかった部分まで範囲に入ってしまい、動かない部分の
    割合が少なくなって判定が安定しないことがある(実機で確認)。範囲を
    自分で細かく選べれば、アニメーションのかかっていない安定した部分
    だけを狙って切り抜ける"""
    clicked = QtCore.Signal(int, int)
    dragged = QtCore.Signal(int, int, int, int)  # x1, y1, x2, y2 (ラベル座標、正規化済み)

    # これ未満の移動量はクリックとして扱う(意図せず数ピクセルだけ
    # 動いてしまった場合の誤操作防止)
    DRAG_THRESHOLD_PX = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press_pos = None
        self._rubber_band = QtWidgets.QRubberBand(QtWidgets.QRubberBand.Rectangle, self)

    def mousePressEvent(self, e):
        self._press_pos = e.position().toPoint()
        self._rubber_band.setGeometry(QtCore.QRect(self._press_pos, QtCore.QSize()))
        self._rubber_band.show()

    def mouseMoveEvent(self, e):
        if self._press_pos is None:
            return
        rect = QtCore.QRect(self._press_pos, e.position().toPoint()).normalized()
        self._rubber_band.setGeometry(rect)

    def mouseReleaseEvent(self, e):
        if self._press_pos is None:
            return
        self._rubber_band.hide()
        release_pos = e.position().toPoint()
        dx = abs(release_pos.x() - self._press_pos.x())
        dy = abs(release_pos.y() - self._press_pos.y())
        if dx < self.DRAG_THRESHOLD_PX and dy < self.DRAG_THRESHOLD_PX:
            self.clicked.emit(self._press_pos.x(), self._press_pos.y())
        else:
            x1, y1 = self._press_pos.x(), self._press_pos.y()
            x2, y2 = release_pos.x(), release_pos.y()
            self.dragged.emit(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        self._press_pos = None


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


class DeviceConnectThread(QtCore.QThread):
    """RecorderDialog用: 端末接続・画面サイズ取得・初回スクショ取得を
    バックグラウンドで行う。u2.connect()はATX-Agentとの疎通確認のため
    単体で数秒かかることがあり、これをUIスレッドで同期的に行うと
    記録ウィンドウの表示自体が数秒固まって見えるため、ウィンドウを
    先に表示してから非同期でこれらを行う"""
    sig_log = QtCore.Signal(str)
    sig_ok = QtCore.Signal(object, int, int, object)  # (d, sw, sh, pil)
    sig_error = QtCore.Signal(str)

    def __init__(self, serial):
        super().__init__()
        self.serial = serial

    def run(self):
        try:
            self.sig_log.emit("端末に接続しています…")
            d = core.connect(self.serial)
            self.sig_log.emit("画面サイズを取得しています…")
            sw, sh = d.window_size()
            self.sig_log.emit("画面を取得しています…")
            pil = d.screenshot()
            self.sig_ok.emit(d, sw, sh, pil)
        except Exception as e:
            self.sig_error.emit(str(e))


# ============================================ 記録ダイアログ（クリック式）
class RecorderDialog(QtWidgets.QDialog):
    def __init__(self, serial, name, tpl_w, tpl_h, parent=None, retake_label=None):
        super().__init__(parent)
        self.setWindowTitle(f"記録: {name}")
        self.serial = serial
        self.name = name
        self.tpl_w = tpl_w
        self.tpl_h = tpl_h
        # 端末接続・画面サイズ取得・初回スクショ取得は、ウィンドウを表示した
        # 直後にバックグラウンドスレッドで行う(_start_connect参照)。
        # u2.connect()はATX-Agentの疎通確認のため単体で数秒かかることがあり、
        # __init__内で同期的に行うとウィンドウの表示自体が固まって見えるため
        self.d = None
        self.sw = None
        self.sh = None
        self._connect_thread = None
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
        # _load_existing()が「続きから」を読み込んだ際の記録時device_size。
        # 接続完了(sw/shが分かった時点)で比較するため、ここでは保持だけする
        self._loaded_device_size = None
        # 撮り直し対象として選ばれているステップ(dict、self.stepsの要素そのもの)。
        # Noneでなければ、次のon_clickは新規ステップ追加ではなく撮り直しとして扱う
        self._retake_step = None
        self._retake_seq = 0  # 撮り直しで書き出すファイル名の重複防止用連番

        root = QtWidgets.QHBoxLayout(self)

        # 左: 端末画面
        self.img = ClickableLabel()
        self.img.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        self.img.clicked.connect(self.on_click)
        self.img.dragged.connect(self.on_drag)
        root.addWidget(self.img)

        # 右: 操作パネル
        side = QtWidgets.QVBoxLayout()
        side.addWidget(QtWidgets.QLabel(
            "画面の上で、操作したいボタンを\n実行したい順にクリック\n"
            "(範囲を自分で決めたいときはドラッグ)"))
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

        self.b_reconnect = QtWidgets.QPushButton("再接続")
        self.b_reconnect.clicked.connect(self._start_connect)
        self.b_refresh = QtWidgets.QPushButton("画面更新")
        self.b_refresh.clicked.connect(self.refresh)
        b_undo = QtWidgets.QPushButton("一つ戻す")
        b_undo.clicked.connect(self.undo)
        self.b_save = QtWidgets.QPushButton("保存して閉じる")
        self.b_save.clicked.connect(self.save)
        b_cancel = QtWidgets.QPushButton("キャンセル")
        # reject()ではなくclose()にすることで、ウィンドウの「×」と挙動を揃える
        # (closeEvent側で未保存の変更があれば確認する)
        b_cancel.clicked.connect(self.close)
        for b, tip in (
            (self.b_reconnect,
             "端末に接続し直します。接続に失敗した時や、USBを挿し直した後に押してください。"),
            (self.b_refresh, "端末の現在の画面を撮り直して表示を更新します。"),
            (b_undo, "直前に記録したステップ、または共通ポップアップを1つ取り消します。"),
            (self.b_save, "ここまで記録した内容をレシピとして保存し、記録ウィンドウを閉じます。"),
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

        # ここまでは端末通信を一切行わないUI構築のみなので、ウィンドウは
        # このコンストラクタが返った直後(exec()呼び出し)にすぐ表示される。
        # 既存レシピの読み込みも画像ファイルは読まない軽い処理なので同期のまま
        # (実測: 500ステップで_load_existing 1ms未満・_refresh_lists 数ms程度。
        # 詳細は対応時の報告を参照)
        self._load_existing(auto_continue=(retake_label is not None))
        self._refresh_lists()
        if retake_label is not None:
            self._arm_retake_by_label(retake_label)

        self.img.setText("端末に接続しています…")
        self._set_device_controls_enabled(False)
        self._start_connect()

    def _set_device_controls_enabled(self, enabled):
        """端末との通信を必要とする操作(画面クリックでの記録・画面更新・保存)を
        まとめて有効/無効にする。接続完了までは無効にしておく"""
        self.img.setEnabled(enabled)
        self.b_refresh.setEnabled(enabled)
        self.b_save.setEnabled(enabled)

    def _start_connect(self):
        """端末接続・画面サイズ取得・初回スクショ取得をバックグラウンドスレッドで
        行う。「再接続」ボタンからも呼ばれる"""
        if self._connect_thread is not None and self._connect_thread.isRunning():
            return  # 二重に接続を開始しない
        self.img.setText("端末に接続しています…")
        self._set_device_controls_enabled(False)
        self.b_reconnect.setEnabled(False)
        self._connect_thread = DeviceConnectThread(self.serial)
        self._connect_thread.sig_log.connect(self._msg)
        self._connect_thread.sig_ok.connect(self._on_connected)
        self._connect_thread.sig_error.connect(self._on_connect_failed)
        self._connect_thread.finished.connect(
            lambda: self.b_reconnect.setEnabled(True))
        self._connect_thread.start()

    def _on_connected(self, d, sw, sh, pil):
        self.d = d
        self.sw, self.sh = sw, sh
        self.pil = pil
        self._msg(f"接続しました（画面サイズ {self.sw}x{self.sh}）")
        if (self._loaded_device_size is not None
                and list(self._loaded_device_size) != [self.sw, self.sh]):
            self._msg(
                f"!! 注意: 記録時({self._loaded_device_size})と今の画面サイズ"
                f"({self.sw},{self.sh})が違います")
        self._render_current_pil()
        self._set_device_controls_enabled(True)

    def _on_connect_failed(self, message):
        self.img.setText(
            f"!! 接続に失敗しました:\n{message}\n\n"
            "端末のUSB接続・USBデバッグ許可を確認し、\n"
            "「再接続」を押してください")
        self._msg(f"!! 接続に失敗しました: {message}")

    def _refresh_lists(self):
        self.list_steps_edit.blockSignals(True)
        self.list_steps_edit.clear()
        for s in self.steps:
            # self.stepsはルート階層のみのフラットなリストだが、続きから記録
            # する既存レシピがif/elseを含む場合、ルート階層にifノードが
            # そのまま混ざって並ぶことがある(【if/elseを含むレシピの続きから
            # 記録】参照)。ifノードはこの画面からは中身を編集できない
            # (テンプレート・座標を持たないため撮り直しの対象にもできない)
            # ので、[if]と分かる表示にした上で編集不可にする
            is_if = s.get("type", "tap") == "if"
            item = QtWidgets.QListWidgetItem(f"[if] {s.get('label', '?')}" if is_if else s["label"])
            if not is_if:
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
            # on_steps_reordered()はitem.data(UserRole)に積んだスナップショットから
            # self.stepsを再構築する。PySide6はsetData(UserRole, dict)を渡すと
            # 中身を複製するだけで元のオブジェクトとは別物になる(実測で確認済み、
            # NodeTreeBuilder/BlockTreeWidgetの側テーブルを使っている理由と同じ)ため、
            # ここでラベルを書き換えた直後にitem側のスナップショットも更新しておかないと、
            # このあと並び替えただけで名前変更が巻き戻ってしまう
            item.setData(QtCore.Qt.UserRole, self.steps[idx])
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

        # ルート階層にifノードが1つでもあれば、このレシピは分岐を含む
        # (ifの中(then/else)にさらにifがあってもルート階層のこのifノード
        # 自体で判定できるので、ここでは深く辿る必要はない)
        has_branches = any(s.get("type") == "if" for s in prev_steps)
        branch_note = (
            "\n\nこのレシピには分岐(if)が含まれています。新しく記録する"
            "ステップは、既存の分岐構造を保ったまま一番後ろに追加されます"
            "(あとで「分岐を編集」画面から好きな位置へ移動できます)。"
            "分岐の中のステップ自体は、この記録画面からは撮り直せません"
            "(「分岐を編集」画面や、記録内容タブの「マスクを調整」を"
            "使ってください)。" if has_branches else "")

        if auto_continue:
            resp = QtWidgets.QMessageBox.Yes
        else:
            resp = QtWidgets.QMessageBox.question(
                self, "既存レシピが見つかりました",
                f"「{self.name}」には既に{len(prev_steps)}ステップ"
                f"・{len(prev_popups)}件の共通ポップアップが記録されています。\n\n"
                "「はい」: プログラムが止まった続きから追加記録する\n"
                "「いいえ」: 最初からやり直す（「保存して閉じる」を押すまでは"
                "元の記録は消えません。キャンセルすれば元のまま残ります。"
                "分岐(if)を含む場合、その構造もファイルもまとめて消えます）"
                + branch_note,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes,
            )
        if resp == QtWidgets.QMessageBox.Yes:
            self.steps = prev_steps
            self.popups = prev_popups
            # 「一つ戻す」の対象は、このセッションで新しく記録した項目だけに
            # 限定する(空で始める)。_historyに元々あった項目まで含めると、
            # 記録した順番とは無関係な順序(steps全部→popups全部)でpopされて
            # しまい、一つ戻すだけで無関係な既存ステップを誤って消してしまう。
            # if/elseを含むレシピでは、これによってthen/elseの中身ごと
            # ブロック全体が消えてしまう恐れがあり被害が大きいため、
            # 既存の項目は「一つ戻す」の対象から外す(消したければ
            # 「分岐を編集」画面や記録内容タブの削除機能を使う)
            self._history = []
            self._msg(
                f"続きから記録します（ステップ{len(prev_steps)}件・"
                f"共通ポップアップ{len(prev_popups)}件を読み込み済み）"
                + ("\n※分岐(if)を含みます。新しいステップは一番後ろに追加され、"
                   "「一つ戻す」は今回新しく記録した項目にしか使えません"
                   if has_branches else ""))
            # この時点では未接続でself.sw/shが分からないため、比較は
            # 接続完了後(_on_connected)に行う。ここでは値を覚えておくだけ
            self._loaded_device_size = data.get("device_size")
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
                "残ります（キャンセルすれば元のまま使えます）"
                + ("\n※分岐(if)を含んでいたレシピです。保存すると、その分岐"
                   "構造とファイルもまとめて失われます" if has_branches else ""))

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
        # 接続中に閉じられた場合、スレッド終了時のシグナルが破棄済みの
        # ウィジェットを触らないよう、先に切り離しておく(接続処理自体は
        # 待たずにそのままバックグラウンドで終わらせる。ウィンドウを
        # 閉じる操作を遅延させたくないため)
        if self._connect_thread is not None and self._connect_thread.isRunning():
            try:
                self._connect_thread.sig_log.disconnect()
                self._connect_thread.sig_ok.disconnect()
                self._connect_thread.sig_error.disconnect()
                self._connect_thread.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
        self.setResult(QtWidgets.QDialog.Rejected)
        event.accept()

    def refresh(self):
        if self.d is None:
            return  # 未接続の間は何もしない(接続完了後に_on_connectedが描画する)
        try:
            self.pil = self.d.screenshot()
        except Exception as e:
            self._msg(f"!! スクショ失敗: {e}")
            return
        self._render_current_pil()

    def _render_current_pil(self):
        """self.pil の内容を画面エリアに描画する(端末通信は行わない)。
        refresh()と接続完了時(_on_connected)の両方から呼ばれる"""
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
            if s.get("type", "tap") != "tap":
                # ifノードはタップ座標(x/y)を持たない(続きから記録している
                # 分岐入りレシピでルート階層に混ざっていることがある)ので、
                # マーカーは描かずスキップする
                continue
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

    def _next_free_index(self, *prefixes):
        """新しいステップ/共通ポップアップ用ファイル名の連番を決める。
        self.steps/self.popupsの件数(len)ではなく、ディスク上に実際に
        存在するファイルから「これまでに使われた最大の番号+1」を返す。

        件数を使わない理由: if/elseを含むレシピを「続きから記録」すると、
        ルート階層の件数(len(self.steps))は、分岐の中に隠れているステップの
        分だけ実際に使われてきたファイル数より少なくなる(例: 5ステップ中
        2つをifのthenにまとめると、ルートは4件になるが、ファイルは
        既に5つ分使われている)。件数ベースで採番すると、その差分の分だけ
        番号が若返り、次の新規ステップが既存の(分岐の中にある)ファイルと
        同じ番号を再利用して上書きしてしまう。StructureEditorDialogでの
        削除(JSONだけ消してファイルは残す)でも同様のズレが起こり得る"""
        max_n = 0
        for prefix in prefixes:
            pattern = re.compile(re.escape(prefix) + r"(\d+)")
            for f in self.dir.glob(f"{prefix}*.png"):
                m = pattern.match(f.stem)
                if m:
                    max_n = max(max_n, int(m.group(1)))
        return max_n + 1

    def _capture_masked_template(self, rx, ry, w=None, h=None):
        """クリック位置(rx, ry)を中心に複数フレーム撮影し、マスク付きZNCC用の
        テンプレート・マスク・自動しきい値・確認用画像(ctx)を作る。

        w, h: 切り抜きサイズ。省略時は「切抜き幅/高さ」欄の値
        (self.tpl_w/self.tpl_h)を使う。矩形ドラッグで範囲を自分で
        指定した場合はここに実際のドラッグサイズが渡される

        新規ステップ/共通ポップアップの記録と、既存ステップの撮り直しの
        両方から呼ばれる共通処理(【撮り直し機能】追加にあたり on_click から
        切り出した)。戻り値: (tpl_gray, mask, dx, dy, computed_threshold, ctx)
        """
        tpl_w = w if w is not None else self.tpl_w
        tpl_h = h if h is not None else self.tpl_h

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
                      (rx - tpl_w // 2, ry - tpl_h // 2),
                      (rx + tpl_w // 2, ry + tpl_h // 2),
                      (0, 0, 255), 4)

        dx = dy = 0
        crops_gray = []
        for f in frames:
            crop_img, (dx, dy) = core.crop(f, rx, ry, tpl_w, tpl_h)
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
        rx = int(lx / self.scale)
        ry = int(ly / self.scale)
        self._handle_capture_point(rx, ry)

    def on_drag(self, lx1, ly1, lx2, ly2):
        """矩形ドラッグで切り抜き範囲を自分で指定した場合。既定の
        「切抜き幅/高さ」は使わず、ドラッグした矩形のサイズをそのまま使う"""
        if self.pil is None:
            return
        rx1, ry1 = int(lx1 / self.scale), int(ly1 / self.scale)
        rx2, ry2 = int(lx2 / self.scale), int(ly2 / self.scale)
        w = max(8, abs(rx2 - rx1))
        h = max(8, abs(ry2 - ry1))
        rx = (rx1 + rx2) // 2
        ry = (ry1 + ry2) // 2
        self._handle_capture_point(rx, ry, w, h)

    def _handle_capture_point(self, rx, ry, w=None, h=None):
        """on_click/on_drag共通の処理。クリック(w=h=None)なら既定サイズ、
        ドラッグならそのサイズで撮影する"""
        self._dirty = True

        if self._retake_step is not None:
            self._do_retake(rx, ry, w, h)
            return

        tpl_gray, mask, dx, dy, computed_threshold, ctx = \
            self._capture_masked_template(rx, ry, w, h)

        if self.ck_popup_mode.isChecked():
            idx = self._next_free_index("popup_", "context_popup_", "mask_popup_")
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
            idx = self._next_free_index("step_", "context_", "mask_")
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
        if self.steps[idx].get("type", "tap") != "tap":
            # ifノードはtemplate/座標を持たないため撮り直せない
            # (【if/elseを含むレシピの続きから記録】参照)
            self._msg("!! [if]の行は撮り直せません(「分岐を編集」画面や"
                       "記録内容タブの「マスクを調整」を使ってください)")
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
            # ifノードとラベルが偶然一致しても撮り直し対象にはしない
            # (該当するtapノードが他になければ「見つからない」扱いにする)
            if s.get("type", "tap") == "tap" and s.get("label") == label:
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

    def _do_retake(self, rx, ry, w=None, h=None):
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
            self._capture_masked_template(rx, ry, w, h)

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
            # (同じ番号を再利用したファイルは新しい内容で上書き済みなので残す)。
            # self.steps + self.popupsをそのまま浅く見るだけだと、if/elseの
            # 中(then/else)にあるtapノードやifの条件画像が拾えず、
            # まだ使われているファイルを「未使用」と誤判定して消してしまう
            # 恐れがあるため、木構造を辿るcore.iter_referenced_imagesを使う
            keep = set(core.iter_referenced_images(self.steps))
            for item in self.popups:
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


# ================================================== 分岐構造(if/else)編集
# if の入れ子はこの画面からは深さMAX_NEST_DEPTHまでしか作れない(警告では
# なく制限)。深くネストしたレシピが期待通り動かなかったとき、現状の
# ツールには原因を切り分ける手段が無いため。JSONを直接編集すれば
# この制限を超えたレシピを作ること自体は可能(再生エンジン側は深さを
# 一切気にしない。core.Cursorのdocstring参照)
MAX_NEST_DEPTH = 3


class BlockTreeWidget(QtWidgets.QTreeWidget):
    """分岐構造編集用のツリー。ドラッグ&ドロップは同一階層(同じ親)内の
    並び替えに限定し、階層をまたぐ移動(別のブロックの中へ入れる/外へ出す)
    はここでは実装しない(フェーズ1aの範囲外)。

    RecorderDialogのReorderableListWidgetと同じ理由(PySide6 6.11で確認)
    で、InternalMoveでの並び替え中はQt内部の実装(行の挿入→削除)により
    itemChangedが「移動前の行」を指したまま一時的に発火することがある。
    これをitemChanged側で見分けるのは難しいため、dropEventの開始～終了を
    droppingフラグで明示し、ハンドラ側でその間は無視できるようにする
    (ツリー表示でも同じ形で起こり得るため、最初からガードを入れておく)"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dropping = False
        # id(item) -> role辞書 の側テーブル。PySide6はQTreeWidgetItem.setData/
        # data(..., UserRole)にPythonのdictを渡すとQVariant経由で中身が複製され、
        # 元のオブジェクトと同一性(is)が保たれない(実測で確認)。そのため
        # ノードやif_nodeへの参照を保持するroleは、Qt側のデータストレージでは
        # なくこちらの側テーブルで管理する(setData自体は一切使わない)
        self._roles = {}
        self.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)

    def set_role(self, item, role):
        self._roles[id(item)] = role

    def get_role(self, item):
        return self._roles.get(id(item))

    def reset_roles(self):
        """clear()等で古いアイテムが破棄される前に呼ぶこと。id()はPythonの
        ラッパーオブジェクトの生存期間中しか一意性を保証しないため、
        再構築のたびに古いエントリを確実に捨てる"""
        self._roles = {}

    def dropEvent(self, event):
        dragged = self.selectedItems()
        if not dragged:
            event.ignore()
            return
        dragged_parent = dragged[0].parent()
        for it in dragged:
            # 複数選択がそもそも別の階層にまたがっている、または
            # then:/else:見出し行そのものが混ざっている場合は拒否
            if it.parent() is not dragged_parent:
                event.ignore()
                return
            role = self.get_role(it)
            if not role or role.get("kind") != "node":
                event.ignore()
                return
        indicator = self.dropIndicatorPosition()
        if indicator == QtWidgets.QAbstractItemView.OnItem:
            # ドロップ先の「子になる」動作(=階層をまたぐ)なので拒否
            event.ignore()
            return
        target_item = self.itemAt(event.position().toPoint())
        target_parent = target_item.parent() if target_item is not None else None
        if target_parent is not dragged_parent:
            event.ignore()
            return
        self.dropping = True
        try:
            super().dropEvent(event)
        finally:
            self.dropping = False


class NodeTreeBuilder:
    """steps(木構造)をBlockTreeWidgetへツリー表示として展開する共通ロジック。
    分岐編集画面(StructureEditorDialog、編集可能)と記録内容タブ(読み取り
    専用)の両方がこれを使う(表示ロジックの重複実装を避けるため)。

    roleには"node"(実データへの参照そのもの。同じ画面内でそのまま編集する
    StructureEditorDialog用)と"path"(トップレベルindex, "then"/"else",
    ブロック内index, ... の形。MaskEditorDialogのように、接続し直さず
    レシピを改めて読み込む側が同じノードを再特定するためのもの。
    core.get_node_by_path()にそのまま渡せる)の両方を積んでおく"""

    def __init__(self, tree, recipe_dir, editable):
        self.tree = tree
        self.recipe_dir = recipe_dir
        self.editable = editable

    def build(self, nodes):
        self.tree.blockSignals(True)
        self.tree.clear()
        self.tree.reset_roles()
        self._build_container(None, nodes, path=(), depth=1)
        self.tree.expandAll()
        self.tree.blockSignals(False)

    def _thumb_icon(self, image_name):
        if not image_name:
            return None
        path = self.recipe_dir / image_name
        if not path.exists():
            return None
        pix = QtGui.QPixmap(str(path))
        if pix.isNull():
            return None
        pix = pix.scaled(48, 48, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        return QtGui.QIcon(pix)

    def _build_container(self, parent_item, nodes, path, depth):
        for i, node in enumerate(nodes):
            node_path = path + (i,)
            ntype = node.setdefault("type", "tap")
            if ntype == "if":
                item = self._make_if_item(node, node_path, depth)
            else:
                item = self._make_tap_item(node, node_path, depth)
            if parent_item is None:
                self.tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)

    def _make_tap_item(self, node, path, depth):
        item = QtWidgets.QTreeWidgetItem([node.get("label", "?")])
        self.tree.set_role(item, {"kind": "node", "node": node, "path": path, "depth": depth})
        if self.editable:
            item.setFlags(item.flags() | QtCore.Qt.ItemIsEditable)
        icon = self._thumb_icon(node.get("context"))
        if icon is not None:
            item.setIcon(0, icon)
        return item

    def _make_if_item(self, node, path, depth):
        item = QtWidgets.QTreeWidgetItem([f"[if] {node.get('label', '?')}"])
        self.tree.set_role(item, {"kind": "node", "node": node, "path": path, "depth": depth})
        if self.editable:
            item.setFlags(item.flags() | QtCore.Qt.ItemIsEditable)
        # ifノード自体は全体スクリーンショット(context)を持たないので、
        # サムネイルはcondition(判定に使う画像)のtemplateを使う
        icon = self._thumb_icon(node.get("condition", {}).get("template"))
        if icon is not None:
            item.setIcon(0, icon)

        then_header = self._make_branch_header(node, "then", depth + 1)
        item.addChild(then_header)
        self._build_container(then_header, node.setdefault("then", []), path + ("then",), depth + 1)

        else_header = self._make_branch_header(node, "else", depth + 1)
        item.addChild(else_header)
        else_list = node.setdefault("else", [])
        if else_list:
            self._build_container(else_header, else_list, path + ("else",), depth + 1)
        else:
            placeholder = QtWidgets.QTreeWidgetItem(
                ["(空。フェーズ1bで実機から追加記録できるようになります)" if self.editable
                 else "(空)"])
            placeholder.setFlags(QtCore.Qt.ItemIsEnabled)
            else_header.addChild(placeholder)
        return item

    def _make_branch_header(self, if_node, branch, depth):
        item = QtWidgets.QTreeWidgetItem(["then:" if branch == "then" else "else:"])
        self.tree.set_role(
            item, {"kind": "branch_header", "if_node": if_node, "branch": branch, "depth": depth})
        item.setFlags((item.flags() & ~QtCore.Qt.ItemIsSelectable) & ~QtCore.Qt.ItemIsDragEnabled)
        font = item.font(0)
        font.setItalic(True)
        item.setFont(0, font)
        return item


class ConditionPickerDialog(QtWidgets.QDialog):
    """ifの条件として使う画像を選ぶ画面。フェーズ1a時点では新規撮影はせず、
    既に撮影済みのステップ、または共通ポップアップとして登録済みの画像の
    template/mask/method/thresholdをそのまま流用する(端末には一切接続しない)"""

    def __init__(self, recipe_dir, steps, popups=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("条件画像を選択")
        self.resize(420, 560)
        self.recipe_dir = recipe_dir
        self.popups = popups if popups is not None else []
        self._extra_result = None  # 失敗履歴から作った場合の合成ステップ

        # 一覧の各行(ステップ・共通ポップアップの両方)に対応する
        # ("step"|"popup", 元のdict) を、Qtの行番号と同じ順序で保持する。
        # QListWidgetItem.setData(UserRole, dict)は、PySide6ではQVariant
        # 経由で中身が複製されるだけで元のオブジェクトと同一ではなくなる
        # (実測で確認済み。BlockTreeWidgetが側テーブルを使っているのと同じ
        # 理由)。共通ポップアップを選んだ場合はself.popupsから同一の
        # オブジェクトをis比較で安全に取り除きたいため、Qt側のデータ機構
        # ではなくこちらのPython側リストで管理する
        self._entries = []

        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(QtWidgets.QLabel(
            "条件として使う画像を選んでください(記録済みのステップ、"
            "または共通ポップアップとして登録済みの画像):"))

        self.list = QtWidgets.QListWidget()
        self.list.setIconSize(QtCore.QSize(48, 48))

        def add_entry(kind, obj, prefix=""):
            item = QtWidgets.QListWidgetItem(f"{prefix}{obj.get('label', '?')}")
            ctx = obj.get("context")
            if ctx:
                path = recipe_dir / ctx
                if path.exists():
                    pix = QtGui.QPixmap(str(path))
                    if not pix.isNull():
                        pix = pix.scaled(48, 48, QtCore.Qt.KeepAspectRatio,
                                          QtCore.Qt.SmoothTransformation)
                        item.setIcon(QtGui.QIcon(pix))
            self.list.addItem(item)
            self._entries.append((kind, obj))

        for step in steps:
            add_entry("step", step)
        if self.popups:
            header = QtWidgets.QListWidgetItem("── 共通ポップアップ ──")
            header.setFlags(QtCore.Qt.ItemIsEnabled)  # 選択不可の見出し行
            font = header.font()
            font.setItalic(True)
            header.setFont(font)
            self.list.addItem(header)
            self._entries.append((None, None))  # 見出し行の分だけ_entriesの行番号を揃える
            for popup in self.popups:
                add_entry("popup", popup, prefix="[ポップアップ] ")
        v.addWidget(self.list, 1)

        b_from_failure = QtWidgets.QPushButton("失敗履歴の画像から選ぶ...")
        b_from_failure.clicked.connect(self.on_pick_from_failure)
        v.addWidget(with_help(
            b_from_failure,
            "既存ステップの画像ではなく、失敗履歴(または繰り返しポップアップ)の"
            "スクリーンショットからドラッグで範囲を選び、それを条件にします。"
            "単一の静止画からの作成のため、判定範囲は全域有効(旧ccoeff相当)"
            "になります。"))

        v.addWidget(help_label(
            "判定条件",
            "「見つかったらthen」: 選んだ画像が画面に見つかったときthenを"
            "実行します。「見つからなかったらthen」: 見つからなかったときに"
            "thenを実行します(例: 通常あるはずのボタンが無いことを検知したい場合)。"
            "いずれの場合もelseは今は空です(フェーズ1bで対応します)。"))
        self.rb_found = QtWidgets.QRadioButton("見つかったら then")
        self.rb_found.setChecked(True)
        self.rb_not_found = QtWidgets.QRadioButton("見つからなかったら then")
        v.addWidget(self.rb_found)
        v.addWidget(self.rb_not_found)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    def on_pick_from_failure(self):
        name = self.recipe_dir.name
        failures = core.load_failures(name)
        if not failures:
            QtWidgets.QMessageBox.information(
                self, "失敗履歴がありません", "このレシピにはまだ失敗履歴がありません。")
            return
        labels = [
            f"{f.get('ts', '?')}  "
            f"{f.get('reason', f.get('popup_label', ''))[:40]}"
            for f in failures
        ]
        chosen, ok = QtWidgets.QInputDialog.getItem(
            self, "失敗履歴を選択", "元にする失敗履歴を選んでください:", labels, 0, False)
        if not ok:
            return
        entry = failures[labels.index(chosen)]
        fname = entry.get("screenshot")
        if not fname:
            QtWidgets.QMessageBox.warning(self, "画像がありません", "この履歴にはスクリーンショットがありません。")
            return
        image_path = self.recipe_dir / fname
        if not image_path.exists():
            QtWidgets.QMessageBox.warning(self, "画像が見つかりません", str(fname))
            return
        try:
            result = ScreenCropDialog.pick(
                image_path, title="条件にする範囲を選択", parent=self)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "読み込み失敗", str(e))
            return
        if result is None:
            return
        # 条件はtemplate/maskだけを使い、タップ位置(cx,cy,dx,dy)は使わない
        tpl_gray, mask, _cx, _cy, _dx, _dy = result
        ts = datetime.datetime.now().strftime("%H%M%S")
        tpl_name = f"cond_from_failure_{ts}.png"
        mask_name = f"cond_from_failure_{ts}_mask.png"
        core.imwrite(self.recipe_dir / tpl_name, tpl_gray)
        core.imwrite(self.recipe_dir / mask_name, mask)
        self._extra_result = {
            "label": f"(失敗履歴より){entry.get('ts', '')}",
            "template": tpl_name, "mask": mask_name,
            "method": "masked_zncc", "threshold": 0.85,
        }
        self.accept()

    def _current_entry(self):
        row = self.list.currentRow()
        if not (0 <= row < len(self._entries)):
            return None, None
        return self._entries[row]  # (kind, obj) または見出し行なら (None, None)

    def accept(self):
        # 共通ポップアップの画像を条件に選んだ場合、そのままだと再生時に
        # 「共通ポップアップ」の割り込み処理がifの条件判定より先に画面を
        # 閉じてしまい、条件が常に不成立になりかねない
        # (【共通ポップアップを条件に使う】参照)。この場に気づける唯一の
        # タイミングなので、選択を確定する瞬間に確認する。
        # _extra_result(失敗履歴からの合成)経由のacceptでは、一覧側の選択は
        # 無関係(たまたま以前選んでいた行が残っているだけ)なので対象外
        kind, obj = (None, None) if self._extra_result is not None else self._current_entry()
        if kind == "popup":
            resp = QtWidgets.QMessageBox.question(
                self, "共通ポップアップを条件に使う",
                f"「{obj.get('label', '?')}」は共通ポップアップとして登録されています。\n\n"
                "登録したままだと、再生中はこの画像が条件判定に到達するより先に"
                "「共通ポップアップ」の割り込み処理で閉じられてしまうため、"
                "この if の条件は常に不成立になる可能性があります。\n\n"
                "共通ポップアップの登録から外しますか？\n"
                "(画像ファイル自体は残ります。「いいえ」を選んでも条件としては"
                "使えますが、上記の理由でうまく動かない可能性があります)",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes)
            if resp == QtWidgets.QMessageBox.Yes:
                # list.remove()の==一致ではなく、is比較で同一オブジェクトのみを
                # 取り除く(内容が偶然同じ別のポップアップを誤って消さないため。
                # on_delete_selectedと同じ考え方)
                for i, p in enumerate(self.popups):
                    if p is obj:
                        del self.popups[i]
                        break
        super().accept()

    def result_value(self):
        if self._extra_result is not None:
            kind = "image_found" if self.rb_found.isChecked() else "image_not_found"
            return self._extra_result, kind
        entry_kind, obj = self._current_entry()
        if obj is None:
            return None
        kind = "image_found" if self.rb_found.isChecked() else "image_not_found"
        return obj, kind

    @staticmethod
    def pick(parent, recipe_dir, steps, popups=None):
        if not steps and not popups:
            QtWidgets.QMessageBox.warning(
                parent, "選べる画像がありません",
                "条件に使えるステップ・共通ポップアップがまだありません。"
                "先に記録画面で記録してください。")
            return None
        dlg = ConditionPickerDialog(recipe_dir, steps, popups, parent)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return None
        result = dlg.result_value()
        if result is None:
            QtWidgets.QMessageBox.warning(parent, "未選択", "画像を選んでください")
            return None
        return result


class StructureEditorDialog(QtWidgets.QDialog):
    """記録済みレシピの分岐構造(if/else)を編集する画面。RecorderDialog
    (記録画面)とは完全に別の画面で、端末には一切接続しない(サムネイルは
    記録済みのcontext画像を読むだけ)。記録画面のコードは一切変更しない。

    フェーズ1a時点でできること:
    - 記録済みのステップ一覧をサムネイル付きツリーで表示
    - 同じ階層で連続選択した範囲を、ifのthenとしてまとめる
      (条件画像は既に撮影済みのステップから選ぶ。新規撮影はしない)
    - elseは空のまま(フェーズ1bで実機からの追加記録に対応)
    - ドラッグ&ドロップは同じ階層内の並び替えのみ(階層をまたぐ移動は
      フェーズ1aの範囲外)
    - ifブロックの解除(elseが空の場合のみ、thenの中身を親階層に戻す)
    - 入れ子は最大MAX_NEST_DEPTH階層(この画面からはそれ以上作れない)"""

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.name = name
        self.recipe_dir = core.recipe_path(name)
        self.setWindowTitle(f"分岐を編集: {name}")
        self.resize(760, 620)
        self._dirty = False

        self.data = self._load_data()

        root = QtWidgets.QHBoxLayout(self)

        self.tree = BlockTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIconSize(QtCore.QSize(48, 48))
        self.tree.itemChanged.connect(self.on_item_changed)
        self.tree.model().rowsMoved.connect(self._on_rows_moved)
        root.addWidget(self.tree, 1)

        side = QtWidgets.QVBoxLayout()
        side.addWidget(help_label(
            "分岐構造の編集",
            "記録済みのステップ一覧をツリーで表示します。ダブルクリックで"
            "名前を変更、ドラッグ&ドロップで同じ階層内の並び替えができます。"))

        b_make_if = QtWidgets.QPushButton("選択範囲を if の then にする")
        b_make_if.clicked.connect(self.on_make_if)
        side.addWidget(with_help(
            b_make_if,
            "同じ階層で連続して選んだステップを、ifブロックのthen(条件が"
            "成立したときに実行する側)としてまとめます。条件画像は既に"
            f"撮影済みのステップから選びます。入れ子は{MAX_NEST_DEPTH}階層"
            "までです(これを超える操作はこの画面からはできません)。"))

        b_unblock = QtWidgets.QPushButton("選択した if ブロックを解除")
        b_unblock.clicked.connect(self.on_unblock)
        side.addWidget(with_help(
            b_unblock,
            "選んだifブロックを外し、thenの中身をそのまま親の階層に戻します。"
            "elseに中身があるifはこの画面からは解除できません。"))

        b_delete = QtWidgets.QPushButton("選択した項目を削除")
        b_delete.setStyleSheet("color: #b00000;")
        b_delete.clicked.connect(self.on_delete_selected)
        side.addWidget(with_help(
            b_delete,
            "選んだステップ・ifブロック(中身ごと)を削除します。複数選択可。"
            "元に戻せません(保存するまでは反映されないので、間違えたら"
            "保存せずに閉じれば取り消せます)。テンプレート画像などのファイル"
            "自体は残ります(他から参照されている可能性があるため)。"))

        side.addStretch(1)

        b_save = QtWidgets.QPushButton("保存")
        b_save.clicked.connect(self.save)
        side.addWidget(b_save)
        b_close = QtWidgets.QPushButton("閉じる")
        b_close.clicked.connect(self.close)
        side.addWidget(b_close)

        root.addLayout(side)

        self._builder = NodeTreeBuilder(self.tree, self.recipe_dir, editable=True)
        self._rebuild_tree()

    # ---------------------------------------------------------- データ入出力
    def _load_data(self):
        path = self.recipe_dir / "recipe.json"
        if not path.exists():
            return {"steps": [], "popups": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("steps", [])
        data.setdefault("popups", [])
        self._normalize_types(data["steps"])
        return data

    def _normalize_types(self, nodes):
        """type未指定のノードをtapとして補完する(load_recipeの
        setdefaultと同じ考え方)。ディスク上のファイルは保存するまで
        書き換えない(このメソッドはメモリ上のself.dataだけを触る)"""
        for node in nodes:
            ntype = node.setdefault("type", "tap")
            if ntype == "if":
                node.setdefault("condition", {})
                self._normalize_types(node.setdefault("then", []))
                self._normalize_types(node.setdefault("else", []))

    def save(self):
        core.save_recipe(self.name, self.data)
        self._dirty = False
        QtWidgets.QMessageBox.information(self, "保存しました", f"「{self.name}」を保存しました")

    def closeEvent(self, event):
        if self._dirty:
            resp = QtWidgets.QMessageBox.question(
                self, "保存されていない変更があります",
                "保存せずに閉じますか？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if resp != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
        event.accept()

    # ---------------------------------------------------------- ツリー構築
    def _rebuild_tree(self):
        self._builder.build(self.data["steps"])

    def _container_of(self, parent_item):
        """parent_item(then:/else:見出し、またはNone=ルート)が表す
        実データ上のリストを返す"""
        if parent_item is None:
            return self.data["steps"]
        role = self.tree.get_role(parent_item)
        return role["if_node"][role["branch"]]

    # ---------------------------------------------------------- 編集操作
    def on_item_changed(self, item, column):
        if self.tree.dropping:
            # ReorderableListWidgetと同じ理由: ドラッグ中の一時的な
            # 誤発火を無視する(Cursor移動前の行を指したitemChangedが
            # ツリー表示でも起こり得るため)
            return
        role = self.tree.get_role(item)
        if not role or role.get("kind") != "node":
            return
        node = role["node"]
        is_if = node.get("type") == "if"
        prefix = "[if] "
        text = item.text(0)
        new_label = text[len(prefix):] if is_if and text.startswith(prefix) else text
        new_label = new_label.strip()
        if new_label:
            node["label"] = new_label
            self._dirty = True
        else:
            # 空にはできない→表示を元に戻す(itemChangedの再発火を避けるため
            # blockSignalsで囲む)
            self.tree.blockSignals(True)
            item.setText(0, (prefix if is_if else "") + node.get("label", "?"))
            self.tree.blockSignals(False)

    def _on_rows_moved(self, *args):
        """ドラッグ&ドロップ後、見た目の並び順を実データ(self.data)へ
        反映する(BlockTreeWidget.dropEventにより同一階層内の移動しか
        起こらないことは保証済み)"""

        def walk(parent_item):
            nodes = []
            if parent_item is None:
                count = self.tree.topLevelItemCount()
                get_child = self.tree.topLevelItem
            else:
                count = parent_item.childCount()
                get_child = parent_item.child
            for i in range(count):
                item = get_child(i)
                role = self.tree.get_role(item)
                if not role or role.get("kind") != "node":
                    continue
                node = role["node"]
                nodes.append(node)
                if node.get("type") == "if":
                    for j in range(item.childCount()):
                        header = item.child(j)
                        hrole = self.tree.get_role(header)
                        if hrole and hrole.get("kind") == "branch_header":
                            node[hrole["branch"]] = walk(header)
            return nodes

        self.data["steps"] = walk(None)
        self._dirty = True

    def on_make_if(self):
        items = [it for it in self.tree.selectedItems()
                 if (self.tree.get_role(it) or {}).get("kind") == "node"]
        if not items:
            QtWidgets.QMessageBox.warning(self, "選択なし", "ifのthenにする範囲を選択してください")
            return
        parent = items[0].parent()
        for it in items:
            if it.parent() is not parent:
                QtWidgets.QMessageBox.warning(self, "選択エラー", "同じ階層の範囲だけ選択してください")
                return
        if parent is None:
            index_of = self.tree.indexOfTopLevelItem
        else:
            index_of = parent.indexOfChild
        indices = sorted(index_of(it) for it in items)
        if indices[-1] - indices[0] + 1 != len(indices):
            QtWidgets.QMessageBox.warning(
                self, "選択エラー", "連続した範囲だけ選択してください(間を飛ばせません)")
            return

        depth = self.tree.get_role(items[0])["depth"]
        if depth + 1 > MAX_NEST_DEPTH:
            QtWidgets.QMessageBox.warning(
                self, "入れ子が深すぎます",
                f"if の入れ子は{MAX_NEST_DEPTH}階層までです。これ以上深く"
                "することはこの画面からはできません"
                "(JSONを直接編集すれば可能ですが、動作の切り分けが難しくなります)。")
            return

        label, ok = QtWidgets.QInputDialog.getText(self, "分岐の名前", "この分岐(if)の名前:")
        if not ok or not label.strip():
            return

        picked = ConditionPickerDialog.pick(
            self, self.recipe_dir, list(core.iter_tap_leaves(self.data["steps"])),
            self.data.get("popups", []))
        if picked is None:
            return
        step, kind = picked
        # template/mask/method/thresholdは値をコピーする(stepオブジェクトへの
        # 参照は持たない)。参照のままだと、後から元のステップ(または共通
        # ポップアップ)を撮り直したときにファイル名が変わり、この条件の
        # 判定内容が利用者の知らないところで勝手に変わってしまうため。
        # 文字列・数値はPythonでは代入時点で値コピーになるので、ここでは
        # 新しいdictを組み立てるだけでよい(stepの辞書オブジェクト自体は
        # 参照しない)
        condition = {
            "kind": kind,
            "template": step["template"],
            "method": step.get("method", "ccoeff"),
            "threshold": step.get("threshold", 0.85),
        }
        if step.get("mask"):
            condition["mask"] = step["mask"]

        items_sorted = sorted(items, key=lambda it: index_of(it))
        selected_nodes = [self.tree.get_role(it)["node"] for it in items_sorted]

        container = self._container_of(parent)
        start, end = indices[0], indices[-1]
        if_node = {"type": "if", "label": label.strip(), "condition": condition,
                   "then": selected_nodes, "else": []}
        container[start:end + 1] = [if_node]
        self._dirty = True
        self._rebuild_tree()

    def on_unblock(self):
        items = [it for it in self.tree.selectedItems()
                 if (self.tree.get_role(it) or {}).get("kind") == "node"]
        if len(items) != 1:
            QtWidgets.QMessageBox.warning(self, "選択エラー", "解除するifブロックを1つだけ選択してください")
            return
        item = items[0]
        role = self.tree.get_role(item)
        node = role["node"]
        if node.get("type") != "if":
            QtWidgets.QMessageBox.warning(self, "選択エラー", "ifブロックを選択してください")
            return
        if node.get("else"):
            QtWidgets.QMessageBox.warning(
                self, "解除できません",
                "elseに中身があるifは、この画面からは解除できません"
                "(then/elseどちらを残すか自動では判断できないため)。")
            return
        parent = item.parent()
        container = self._container_of(parent)
        # list.index()は==での一致判定のため、内容が偶然同じ(ラベル・条件・
        # then中身が全て等しい)別のifノードを誤って解除してしまう恐れがある。
        # is での同一オブジェクト判定に限定する(on_delete_selectedと同じ考え方)
        idx = next(i for i, n in enumerate(container) if n is node)
        container[idx:idx + 1] = node.get("then", [])
        self._dirty = True
        self._rebuild_tree()

    def on_delete_selected(self):
        items = [it for it in self.tree.selectedItems()
                 if (self.tree.get_role(it) or {}).get("kind") == "node"]
        if not items:
            QtWidgets.QMessageBox.warning(self, "選択なし", "削除する項目を選択してください")
            return
        labels = [self.tree.get_role(it)["node"].get("label", "?") for it in items]
        resp = QtWidgets.QMessageBox.question(
            self, "削除の確認",
            "以下を削除します(中身ごと、元に戻せません):\n"
            + "\n".join(f"・{lbl}" for lbl in labels),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if resp != QtWidgets.QMessageBox.Yes:
            return
        for it in items:
            role = self.tree.get_role(it)
            node = role["node"]
            container = self._container_of(it.parent())
            # 内容が同じノードが複数あっても取り違えないよう、同一オブジェクト
            # (is)で探す(list.remove/index は == で探すため、値が同じ別ノードを
            # 誤って消してしまう恐れがある)
            for i, n in enumerate(container):
                if n is node:
                    del container[i]
                    break
        self._dirty = True
        self._rebuild_tree()


# ================================================ オフラインでのマスク調整
class MaskEditorDialog(QtWidgets.QDialog):
    """すでに記録済みのステップ/共通ポップアップについて、端末に接続し
    直さずに、保存済みのテンプレート画像に対して判定範囲(マスク)だけを
    調整する画面。

    記録・撮り直し(RecorderDialogの多フレーム撮影)は、その場で新しい
    範囲の動き/静止を実測できる。これに対してこの画面は"すでに撮影済みの
    画像"だけを使うので、新たに動き情報を取得することはできない。

    記録時に保存されたcontext画像(端末の全体スクリーンショット1枚)を
    表示に使い、画面のどこでも新しく範囲を選び直せるようにしている。
    ただし元々の切り抜き範囲(複数フレームから動き/静止を実測済み)の
    "外側"を選んだ場合、その部分は1枚の静止画像しかないため安定性が
    未検証であり、判定に使う範囲として全面的に有効(旧ccoeff方式と同じ
    扱い)として扱う。元の切り抜き範囲の"内側"は、実測済みのマスクを
    そのまま引き継ぐ(狭めることはできるが、実測で不安定と分かった
    画素を後から安定していたことにはできない)。しきい値は自動では
    再計算せず(新しいフレームが無いため)、手動で調整できるようにする
    だけに留める"""

    # 大きな全体スクリーンショットをそのまま等倍で表示すると画面に収まらない
    # ことが多いため、この高さに収まるよう縮小/拡大する目安
    TARGET_DISPLAY_HEIGHT = 900

    def __init__(self, name, kind, target, parent=None):
        """target: kind=="popups"のときは行番号(int、popupsは常にフラットな
        配列なのでこれで一意に特定できる)。kind=="steps"のときは木構造上の
        path(タプル。core.get_node_by_path参照。if/elseが混ざったstepsを
        行番号で特定すると、ズレて別のステップを書き換えてしまうため)"""
        super().__init__(parent)
        self.name = name
        self.kind = kind  # "steps" or "popups"
        self.target = target
        self.recipe_dir = core.recipe_path(name)
        self.setWindowTitle(f"マスクを調整: {name}")

        self.full_data = json.loads((self.recipe_dir / "recipe.json").read_text(encoding="utf-8"))
        if self.kind == "steps":
            self.node = core.get_node_by_path(self.full_data["steps"], self.target)
        else:
            self.node = self.full_data["popups"][self.target]

        tpl_path = self.recipe_dir / self.node["template"]
        orig_tpl_gray = core.imread(tpl_path, cv2.IMREAD_GRAYSCALE)
        if orig_tpl_gray is None:
            raise FileNotFoundError(f"テンプレート画像が読み込めません: {tpl_path}")
        oh, ow = orig_tpl_gray.shape

        mask_name = self.node.get("mask")
        orig_mask = None
        if mask_name:
            orig_mask = core.imread(self.recipe_dir / mask_name, cv2.IMREAD_GRAYSCALE)
        if orig_mask is None or orig_mask.shape != (oh, ow):
            # マスク未設定(旧ccoeff形式)、または不整合な場合は「全域が
            # 有効」として扱う
            orig_mask = np.full((oh, ow), 255, dtype=np.uint8)

        ctx_name = self.node.get("context")
        ctx_path = self.recipe_dir / ctx_name if ctx_name else None
        ctx_bgr = None
        if ctx_path is not None and ctx_path.exists():
            ctx_bgr = core.imread(ctx_path, cv2.IMREAD_COLOR)

        if ctx_bgr is not None:
            full_gray = cv2.cvtColor(ctx_bgr, cv2.COLOR_BGR2GRAY)
            fh, fw = full_gray.shape
            cx = int(self.node.get("x", fw // 2)) - int(self.node.get("dx", 0))
            cy = int(self.node.get("y", fh // 2)) - int(self.node.get("dy", 0))
            ox1 = max(0, min(cx - ow // 2, fw - ow))
            oy1 = max(0, min(cy - oh // 2, fh - oh))
            # 元の切り抜き範囲の外側は1枚の静止画しか無い(安定性が未検証)
            # ため、テンプレート値はそのままの画素・マスクは全域有効とする。
            # 内側だけは実測済みの多フレーム平均・マスクで上書きする
            self.full_tpl = full_gray.copy()
            self.full_tpl[oy1:oy1 + oh, ox1:ox1 + ow] = orig_tpl_gray
            self.mask = np.full((fh, fw), 255, dtype=np.uint8)
            self.mask[oy1:oy1 + oh, ox1:ox1 + ow] = orig_mask
            self.active_rect = (ox1, oy1, ox1 + ow, oy1 + oh)
        else:
            # context画像が無い(古いレシピ等)場合は、従来通りテンプレート
            # 単体だけを対象にする
            self.full_tpl = orig_tpl_gray
            self.mask = orig_mask.copy()
            self.active_rect = (0, 0, ow, oh)

        self._orig_mask = self.mask.copy()
        self._orig_active_rect = self.active_rect

        # 記録済みのdx/dyから、現在の座標系(full_tpl)上でのタップ位置を
        # 逆算しておく。dx=dy=0(=範囲の中心をそのままタップ)の場合は
        # tap_pos=Noneとし、「範囲の中心を使う」既定動作のまま扱う
        node_dx = int(self.node.get("dx", 0))
        node_dy = int(self.node.get("dy", 0))
        if node_dx or node_dy:
            rx1, ry1, rx2, ry2 = self.active_rect
            rcx, rcy = (rx1 + rx2) // 2, (ry1 + ry2) // 2
            th, tw = self.full_tpl.shape
            self.tap_pos = (max(0, min(rcx + node_dx, tw - 1)),
                             max(0, min(rcy + node_dy, th - 1)))
        else:
            self.tap_pos = None
        self._orig_tap_pos = self.tap_pos
        self._dirty = False

        fh, fw = self.full_tpl.shape
        self.preview_scale = max(0.2, min(3.0, self.TARGET_DISPLAY_HEIGHT / fh))

        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(QtWidgets.QLabel(
            f"「{self.node.get('label', '?')}」の記録時の全体スクリーンショットです。"
            "黄色い枠が現在の判定範囲、赤色が枠の中で「判定から除外されている」"
            "範囲です。ドラッグで新しく範囲を選び直せます(元の枠の外側は1枚の"
            "静止画像しかないため、安定性は未検証のまま全面有効になります)。"
            "タップしたい位置が範囲の中心と違う場合は、1点クリックして"
            "個別に指定してください(緑の十字で表示されます)。"))

        self.preview = ClickableLabel()
        self.preview.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        self.preview.dragged.connect(self.on_drag)
        self.preview.clicked.connect(self.on_click_tap_pos)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(self.preview)
        scroll.setWidgetResizable(False)
        v.addWidget(scroll, 1)

        self.lbl_ratio = QtWidgets.QLabel()
        v.addWidget(self.lbl_ratio)

        tap_row = QtWidgets.QHBoxLayout()
        b_reset_tap = QtWidgets.QPushButton("タップ位置を範囲の中心に戻す")
        b_reset_tap.clicked.connect(self.on_reset_tap)
        tap_row.addWidget(b_reset_tap)
        v.addLayout(tap_row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(help_label(
            "しきい値",
            "この場では新しい撮影はしないため自動では再計算しません。"
            "必要なら手動で調整してください(既存の値のままでも構いません)。"))
        self.sp_threshold = QtWidgets.QDoubleSpinBox()
        self.sp_threshold.setRange(0.0, 1.0)
        self.sp_threshold.setSingleStep(0.01)
        self.sp_threshold.setDecimals(3)
        self.sp_threshold.setValue(float(self.node.get("threshold", 0.85)))
        row.addWidget(self.sp_threshold)
        v.addLayout(row)

        btn_row = QtWidgets.QHBoxLayout()
        b_reset = QtWidgets.QPushButton("元に戻す")
        b_reset.clicked.connect(self.on_reset)
        btn_row.addWidget(b_reset)
        b_save = QtWidgets.QPushButton("保存")
        b_save.clicked.connect(self.save)
        btn_row.addWidget(b_save)
        b_close = QtWidgets.QPushButton("閉じる")
        b_close.clicked.connect(self.close)
        btn_row.addWidget(b_close)
        v.addLayout(btn_row)

        self.resize(min(1000, int(fw * self.preview_scale) + 60), 700)
        self._render()

    def _render(self):
        h, w = self.full_tpl.shape
        bgr = cv2.cvtColor(self.full_tpl, cv2.COLOR_GRAY2BGR)
        # 判定に使う(有効な)範囲は元の画像のまま見せる(全域が有効な初期状態で
        # 画像全体が色に埋もれてしまうと、どこを切り取ればいいか分からなく
        # なるため)。除外された範囲だけ赤く着色して分かるようにする
        blended = bgr.copy()
        excluded = self.mask == 0
        if excluded.any():
            red_overlay = np.zeros_like(bgr)
            red_overlay[:, :] = (0, 0, 255)
            tinted = cv2.addWeighted(bgr, 0.45, red_overlay, 0.55, 0)
            blended[excluded] = tinted[excluded]
        x1, y1, x2, y2 = self.active_rect
        cv2.rectangle(blended, (x1, y1), (x2, y2), (0, 255, 255), 2)
        if self.tap_pos is not None:
            cv2.drawMarker(blended, self.tap_pos, (0, 255, 0), cv2.MARKER_CROSS, 24, 2)
        scale = self.preview_scale
        interp = cv2.INTER_NEAREST if scale >= 1 else cv2.INTER_AREA
        big = cv2.resize(blended, (max(1, int(w * scale)), max(1, int(h * scale))),
                          interpolation=interp)
        big = np.ascontiguousarray(big)
        qimg = QtGui.QImage(big.data, big.shape[1], big.shape[0],
                             big.strides[0], QtGui.QImage.Format_BGR888)
        self.preview.setPixmap(QtGui.QPixmap.fromImage(qimg.copy()))
        self.preview.resize(big.shape[1], big.shape[0])
        x1, y1, x2, y2 = self.active_rect
        sub_mask = self.mask[y1:y2, x1:x2]
        ratio = float((sub_mask > 0).mean()) if sub_mask.size else 0.0
        tap = self.tap_pos or ((x1 + x2) // 2, (y1 + y2) // 2)
        tap_note = "" if self.tap_pos is not None else "(範囲の中心)"
        self.lbl_ratio.setText(
            f"現在の判定範囲: {x2 - x1}×{y2 - y1}px / 有効画素率: {ratio * 100:.1f}% / "
            f"タップ位置: {tap[0]},{tap[1]} {tap_note}")

    def on_drag(self, dx1, dy1, dx2, dy2):
        scale = self.preview_scale
        x1, y1, x2, y2 = (int(v / scale) for v in (dx1, dy1, dx2, dy2))
        h, w = self.mask.shape
        x1, x2 = max(0, x1), min(w, x2)
        y1, y2 = max(0, y1), min(h, y2)
        if x2 - x1 < 8 or y2 - y1 < 8:
            return  # 小さすぎる選択は誤操作とみなして無視する
        new_mask = np.zeros_like(self.mask)
        new_mask[y1:y2, x1:x2] = self.mask[y1:y2, x1:x2]
        self.mask = new_mask
        self.active_rect = (x1, y1, x2, y2)
        self._dirty = True
        self._render()

    def on_click_tap_pos(self, dx, dy):
        scale = self.preview_scale
        h, w = self.full_tpl.shape
        x = max(0, min(int(dx / scale), w - 1))
        y = max(0, min(int(dy / scale), h - 1))
        self.tap_pos = (x, y)
        self._dirty = True
        self._render()

    def on_reset_tap(self):
        self.tap_pos = None
        self._dirty = True
        self._render()

    def on_reset(self):
        self.mask = self._orig_mask.copy()
        self.active_rect = self._orig_active_rect
        self.tap_pos = self._orig_tap_pos
        self._dirty = True
        self._render()

    def save(self):
        x1, y1, x2, y2 = self.active_rect
        sub_mask = self.mask[y1:y2, x1:x2]
        if not (sub_mask > 0).any():
            QtWidgets.QMessageBox.warning(
                self, "保存できません", "有効な範囲が0になっています。範囲を選び直してください。")
            return
        sub_tpl = self.full_tpl[y1:y2, x1:x2]
        mask_name = self.node.get("mask")
        if not mask_name:
            mask_name = pathlib.Path(self.node["template"]).stem + "_mask.png"
        core.imwrite(self.recipe_dir / self.node["template"], sub_tpl)
        core.imwrite(self.recipe_dir / mask_name, sub_mask)
        self.node["mask"] = mask_name
        self.node["method"] = "masked_zncc"
        self.node["threshold"] = self.sp_threshold.value()
        # タップ位置を独立して指定していない場合は、従来通り範囲の中心を
        # タップ位置として使う。指定している場合は、範囲の中心からの
        # ずれ(dx/dy)として保存し、再生時は検出位置+dx/dyでタップする
        center_x, center_y = (x1 + x2) // 2, (y1 + y2) // 2
        tap_x, tap_y = self.tap_pos or (center_x, center_y)
        self.node["x"] = tap_x
        self.node["y"] = tap_y
        self.node["dx"] = tap_x - center_x
        self.node["dy"] = tap_y - center_y
        core.save_recipe(self.name, self.full_data)
        self._dirty = False
        QtWidgets.QMessageBox.information(self, "保存しました", "マスクを更新しました。")

    def closeEvent(self, event):
        if self._dirty:
            resp = QtWidgets.QMessageBox.question(
                self, "保存されていない変更があります",
                "保存せずに閉じますか？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if resp != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
        event.accept()


# ============================================ 画像1枚からの範囲選択(共通部品)
class ScreenCropDialog(QtWidgets.QDialog):
    """画像1枚(失敗履歴のスクリーンショット、既存ステップのcontextなど)を
    表示し、ドラッグで選んだ範囲(検出に使う画像)と、クリックで指定した
    タップ位置を切り出して返す共通部品。

    検出範囲とタップ位置は別々に指定できる(例: 「フレンド申請しました」の
    文字を検出範囲にしつつ、実際にタップしたいのは少し離れた場所にある
    「閉じる」ボタン、というケース)。クリックしなければ検出範囲の中心を
    タップ位置として使う。

    ファイルの読み書きは一切行わない(結果を使って何をするかは呼び出し側
    次第: 新規ステップとして保存する、条件として使う、など)。単一フレーム
    の画像しか無いため、マスクは常に全域有効(旧ccoeff相当)で返す"""

    TARGET_DISPLAY_HEIGHT = 900

    def __init__(self, image_path, title="範囲を選択", message=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        bgr = core.imread(image_path, cv2.IMREAD_COLOR)
        if bgr is None:
            raise FileNotFoundError(f"画像が読み込めません: {image_path}")
        self.full_gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        self.rect = None
        self.tap_pos = None  # (x, y) 未指定ならacceptで検出範囲の中心を使う
        self.result_data = None  # accept()成功後に (tpl_gray, mask, cx, cy) をセット

        fh, fw = self.full_gray.shape
        self.preview_scale = max(0.2, min(3.0, self.TARGET_DISPLAY_HEIGHT / fh))

        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(QtWidgets.QLabel(
            message or
            "検出に使う範囲をドラッグで選んでください。この画像は1枚の"
            "静止画なので、選んだ範囲の判定は全域有効(旧ccoeff相当)になります"
            "(赤・緑・黄色のマーカーが写っている場合は、かからない範囲を"
            "選んでください)。タップしたい位置が範囲の中心と違う場合は、"
            "1点クリックして個別に指定してください(緑の十字で表示されます)。"))

        self.preview = ClickableLabel()
        self.preview.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        self.preview.dragged.connect(self.on_drag)
        self.preview.clicked.connect(self.on_click_tap_pos)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(self.preview)
        scroll.setWidgetResizable(False)
        v.addWidget(scroll, 1)

        self.lbl_info = QtWidgets.QLabel("範囲を選んでください")
        v.addWidget(self.lbl_info)

        b_reset_tap = QtWidgets.QPushButton("タップ位置を範囲の中心に戻す")
        b_reset_tap.clicked.connect(self.on_reset_tap)
        v.addWidget(b_reset_tap)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        self.btn_ok = btns.button(QtWidgets.QDialogButtonBox.Ok)
        self.btn_ok.setEnabled(False)
        v.addWidget(btns)

        self.resize(min(1000, int(fw * self.preview_scale) + 60), 700)
        self._render()

    def _render(self):
        bgr = cv2.cvtColor(self.full_gray, cv2.COLOR_GRAY2BGR)
        if self.rect is not None:
            x1, y1, x2, y2 = self.rect
            cv2.rectangle(bgr, (x1, y1), (x2, y2), (0, 255, 255), 2)
        if self.tap_pos is not None:
            cv2.drawMarker(bgr, self.tap_pos, (0, 255, 0), cv2.MARKER_CROSS, 24, 2)
        h, w = self.full_gray.shape
        scale = self.preview_scale
        interp = cv2.INTER_NEAREST if scale >= 1 else cv2.INTER_AREA
        big = cv2.resize(bgr, (max(1, int(w * scale)), max(1, int(h * scale))),
                          interpolation=interp)
        big = np.ascontiguousarray(big)
        qimg = QtGui.QImage(big.data, big.shape[1], big.shape[0],
                             big.strides[0], QtGui.QImage.Format_BGR888)
        self.preview.setPixmap(QtGui.QPixmap.fromImage(qimg.copy()))
        self.preview.resize(big.shape[1], big.shape[0])
        self._update_info()

    def _update_info(self):
        if self.rect is None:
            self.lbl_info.setText("範囲を選んでください")
            return
        x1, y1, x2, y2 = self.rect
        tap = self.tap_pos or ((x1 + x2) // 2, (y1 + y2) // 2)
        tap_note = "" if self.tap_pos is not None else "(範囲の中心)"
        self.lbl_info.setText(
            f"検出範囲: {x2 - x1}×{y2 - y1}px  /  タップ位置: {tap[0]},{tap[1]} {tap_note}")

    def on_drag(self, dx1, dy1, dx2, dy2):
        scale = self.preview_scale
        x1, y1, x2, y2 = (int(v / scale) for v in (dx1, dy1, dx2, dy2))
        h, w = self.full_gray.shape
        x1, x2 = max(0, x1), min(w, x2)
        y1, y2 = max(0, y1), min(h, y2)
        if x2 - x1 < 8 or y2 - y1 < 8:
            return
        self.rect = (x1, y1, x2, y2)
        self.btn_ok.setEnabled(True)
        self._render()

    def on_click_tap_pos(self, dx, dy):
        scale = self.preview_scale
        h, w = self.full_gray.shape
        x = max(0, min(int(dx / scale), w - 1))
        y = max(0, min(int(dy / scale), h - 1))
        self.tap_pos = (x, y)
        self._render()

    def on_reset_tap(self):
        self.tap_pos = None
        self._render()

    def accept(self):
        if self.rect is None:
            QtWidgets.QMessageBox.warning(self, "未選択", "検出範囲を選んでください")
            return
        x1, y1, x2, y2 = self.rect
        tpl = self.full_gray[y1:y2, x1:x2].copy()
        mask = np.full(tpl.shape, 255, dtype=np.uint8)
        center_x, center_y = (x1 + x2) // 2, (y1 + y2) // 2
        tap_x, tap_y = self.tap_pos or (center_x, center_y)
        # dx/dyは「検出した範囲の中心」から「実際にタップしたい位置」への
        # ずれ(core.cropが端寄せ補正に使うのと同じ意味)。再生時はcx,cyの
        # 位置(=マッチした範囲の中心)にこのずれを足してタップするため、
        # タップ位置を範囲の中心と別に指定した場合はここで必ず反映させる
        dx, dy = tap_x - center_x, tap_y - center_y
        self.result_data = (tpl, mask, tap_x, tap_y, dx, dy)
        super().accept()

    @staticmethod
    def pick(image_path, title="範囲を選択", message=None, parent=None):
        """(tpl_gray, mask, tap_x, tap_y, dx, dy) を返す。キャンセル/失敗時はNone。
        tap_x, tap_yはこの画像上での絶対位置(表示・参考用)、dx, dyは
        「検出範囲の中心」から「タップ位置」へのずれで、recipe.jsonの
        step["dx"]/step["dy"]にそのまま書ける値"""
        dlg = ScreenCropDialog(image_path, title, message, parent)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return None
        return dlg.result_data


# ============================================================ 再生スレッド
class PlayerThread(QtCore.QThread):
    sig_log = QtCore.Signal(str)
    sig_cycle = QtCore.Signal(int, int, int)   # (成功, 失敗, 不完全)
    sig_progress = QtCore.Signal(int, int, float, float)  # (今の周, 目標周(0=無限), 経過秒, 平均秒/周)
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

    # 同一周回・同一ポップアップの検知回数がこれを超えたら、原因調査用に
    # その瞬間の画面を診断画像として保存する。実機で「同じポップアップが
    # 繰り返し検知され、本来待っているステップの画面に到達できない」事象を
    # 確認したが、ポップアップの検知・クローズ自体は失敗として扱われないため
    # (閉じる動作そのものは毎回成功している)、従来のfailures.jsonl(失敗時
    # のみ記録)には証拠が残らなかった。これはその穴を埋めるためのもので、
    # ok/ng集計やmax_failによるリトライ停止判定には一切関与しない
    POPUP_REPEAT_ALERT_THRESHOLD = 3
    # 閾値を超えた後も無制限に保存し続けると、放置実行時にディスクを
    # 圧迫するため、同一周回・同一ポップアップにつき保存枚数の上限を設ける
    POPUP_REPEAT_MAX_SCREENSHOTS = 3

    # window_size空間への変換倍率(横×scale_x, 縦×scale_y)の相対差がこれを
    # 超えたら「アスペクト比が違う」として警告する。ステータスバー分の数px
    # の差やDPI丸めなど、良性の誤差は数%程度に収まることが多いのに対し、
    # 縦横比が崩れる典型例(記録時と再生時で画面の向きが違う、解像度設定を
    # 上書きした等)はscale_xとscale_yが数十%〜数倍単位で乖離するため、
    # 誤検知と見逃しのバランスを見て15%に設定した
    ASPECT_MISMATCH_THRESHOLD = 0.15

    # スキップ機能(対象ステップが見つからない間に、レシピ内の後続ステップが
    # 写っていないか探して復帰する仕組み)が、実機ログで無関係な離れた
    # ステップに飛んで周回を破綻させる事例を確認したため、以下で絞り込む。
    # - SKIP_SEARCH_RANGE: 「これより後」を無制限に探さず、直後何ステップ
    #   までに限定する
    # - SKIP_THRESHOLD_MARGIN: 誤って飛ぶコストが高いため、通常より
    #   しきい値を厳しくする
    SKIP_SEARCH_RANGE = 2
    SKIP_THRESHOLD_MARGIN = 0.05

    # タップ後の消失確認をポーリングする間隔(秒)と、打ち切るまでの上限
    # (「タップ後待ち秒」の何倍か)。実機ログで、遷移アニメーションの途中を
    # 1回だけ見て「まだ残っている」と誤判定し、無駄な押し直しが起きる事例
    # (押した直後より一致度が上がっているケース)を確認したため、消えるまで
    # (または上限まで)短い間隔で見続けるようにした。消えた時点で即座に
    # 次へ進めるので、正常時はむしろ従来より速くなる
    TAP_VERIFY_POLL_INTERVAL = 0.3
    TAP_VERIFY_POLL_MAX_MULTIPLIER = 2.0

    # ステップ待ちがこの秒数を超えても対象が現れない場合、「各ステップ最大
    # 待ち秒」(既定300秒)の失敗確定を待たず、一度だけ「進んでいません」と
    # 早期に通知する(放置運用で5分間気づけないのは不便なため)。この後も
    # 待機自体は続け、最終的に失敗すれば別途失敗通知が出る。
    # 「各ステップ最大待ち秒」をこれ未満に設定した場合は、先にタイムアウトの
    # 方が来るため、この通知は出ない(特別扱いは不要)
    STALL_NOTIFY_SECONDS = 120

    def __init__(self, serial, name, loops, threshold_offset,
                 step_timeout, after, poll, jitter, max_fail,
                 verify=True, tap_retry=3, hold_ms=0,
                 notify_fail=True, notify_done=True,
                 notify_disconnect=True, notify_stall=True):
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
        self.notify_fail = notify_fail
        self.notify_done = notify_done
        self.notify_disconnect = notify_disconnect
        self.notify_stall = notify_stall
        self._serial = serial
        self._stop = False
        self._log_file = None
        self._last_popup_check = 0.0
        # 直近の失敗で「どの手法がどれだけ迫っていたか」を保持しておき、
        # failures.jsonlへの記録(【実装3】)に使う
        self._last_attempts = None
        # 診断用: 直近のステップ待ちで最も一致した位置(method, val, cx, cy)と、
        # 実際にタップした座標(cx, cy)。どちらもスクリーンショット空間。
        # 失敗時のスクリーンショットへの位置描画に使う
        self._last_best_loc = None
        self._last_tapped_pos = None
        # 表示解像度(adb inputが解釈する座標系)。run()の冒頭でd.window_size()
        # から設定される。タップ直前のスクショ空間→表示解像度変換に使う
        self.sw = None
        self.sh = None
        # 通知用の状態。_current_cycleはその時点の周回数(通知本文に使う)。
        # _disconnect_notifiedは「接続切れ」通知の連投を防ぐためのフラグで、
        # 端末との通信が一度でも回復すれば(back操作が成功すれば)Falseに戻す
        self._current_cycle = 0
        self._disconnect_notified = False
        # 共通ポップアップの繰り返し検知を周回ごとに数えるための状態。
        # どちらも{ポップアップのlabel: 回数}で、周回の頭(run())で
        # 空にリセットする(周回をまたいで引き継がない)
        self._popup_repeat_counts = {}
        self._popup_repeat_saved = {}

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
            - attempts: [(method, val, threshold, peak_cx, peak_cy), ...]
              試した手法すべての記録(ログ・failures.jsonl用)。peak_cx/cyは
              しきい値に関わらない最も一致した位置(診断用、失敗時の
              スクリーンショットへの位置描画に使う。該当なしならNone)
        """
        method = cand.get("method", "ccoeff")
        thr = self._effective_threshold(cand)
        mask = cand.get("_mask") if method == "masked_zncc" else None
        pcx, pcy, val = core.peak_match(gray, cand["_gray"], method=method, mask=mask)
        found = pcx is not None and val >= thr
        attempts = [(method, val, thr, pcx, pcy)]
        if found:
            return pcx, pcy, val, method, thr, attempts
        if method == "masked_zncc":
            edge_thr = self._effective_threshold_edge(cand)
            ecx, ecy, eval_ = core.peak_match(gray, cand["_gray"], method="edge")
            edge_found = ecx is not None and eval_ >= edge_thr
            attempts.append(("edge", eval_, edge_thr, ecx, ecy))
            if edge_found:
                return ecx, ecy, eval_, "edge", edge_thr, attempts
        return None, None, val, method, thr, attempts

    def _attempts_summary(self, best_by_method):
        """{手法名: (最高一致度, しきい値)} を failures.jsonl 用のJSON化しやすい
        リスト形式に変換する(【実装3】)"""
        return [{"method": m, "score": round(float(v), 4), "threshold": round(float(t), 4)}
                for m, (v, t) in best_by_method.items()]

    def _find_best_match(self, gray, candidates):
        """candidates のうち今の画面に写っているものを探す(一致度が最も高いものを返す)。
        戻り値は (candidates内でのインデックス, 候補dict, cx, cy, val,
        method_used, thr_used, attempts) または見つからなければ None。
        ほぼ無地のテンプレートは暗転画面などに誤検知しやすいため対象から除外する。

        candidatesにifノードが混ざっていた場合、そこで探索を打ち切る(それより
        先の候補は見ない)。ifノードは"_gray"を持たないのでそのまま扱うと
        KeyErrorになる、という理由だけでなく、そもそも「ifを飛び越して
        その先のtapへスキップする」こと自体を許してはいけない。ifは条件判定に
        よってthen/elseどちらへ進むか決めるためのノードであり、それを
        素通りしてしまうと分岐そのものが評価されずに無視されてしまう。
        siblings_ahead()が返す候補は常に「今のノードから見て手前から順」なので、
        先頭からこの位置まで(if本体を含まない)だけを候補にすれば安全"""
        best = None
        for i, s in enumerate(candidates):
            if s.get("type", "tap") != "tap":
                break
            if not core.is_distinctive(s["_gray"]):
                continue
            cx, cy, val, method_used, thr_used, attempts = self._match_candidate(gray, s)
            if cx is not None and (best is None or val > best[4]):
                best = (i, s, cx, cy, val, method_used, thr_used, attempts)
        return best

    def _dismiss_popup_if_any(self, gray, popups, waiting_step_label):
        """共通ポップアップ(広告・フレンド申請等)が写っていれば閉じる。閉じたらTrue

        waiting_step_label: このチェックの時点で本来待っていたステップのlabel。
        繰り返し検知のログ・診断記録(【実装: 繰り返し検知の記録】)に使うだけで、
        検知・クローズの判定そのものには使わない"""
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

        label = popup["label"]
        count = self._popup_repeat_counts.get(label, 0) + 1
        self._popup_repeat_counts[label] = count

        self._log(
            f"    !! 共通ポップアップ「{label}」を検知"
            f"(一致{pval:.4f}[{pmethod}]){fallback_note}したので閉じました"
            f" (タップ{tx},{ty})(この周で{count}回目)")

        # 誤検出ではなく実際に繰り返し表示されている場合の証拠を残す。
        # 検知・クローズ自体は毎回成功している(＝失敗ではない)ので、
        # ok/ng集計やmax_failのリトライ判定には一切影響させない
        if count > self.POPUP_REPEAT_ALERT_THRESHOLD:
            saved = self._popup_repeat_saved.get(label, 0)
            if saved < self.POPUP_REPEAT_MAX_SCREENSHOTS:
                self._popup_repeat_saved[label] = saved + 1
                self._save_popup_repeat_diagnostic(
                    gray, popup, pcx, pcy, pval, pmethod, pthr,
                    jx, jy, count, waiting_step_label)

        time.sleep(self.after)
        return True

    def _save_popup_repeat_diagnostic(self, gray, popup, pcx, pcy, pval, pmethod, pthr,
                                       tapped_x, tapped_y, count, waiting_step_label):
        """繰り返し検知の瞬間の画面を診断画像として保存し、failures.jsonlにも
        記録する。保存に失敗してもここで例外を飲み込み、再生を止めない
        (呼び出し元は既にポップアップを閉じてタップ送信まで終えているため、
        記録に失敗したからといって再生継続を妨げるべきではない)。

        gray(既にマッチングに使った画面)をそのままBGR化して使う。新たに
        スクリーンショットを撮り直さないのは、(1) 実際に判定した瞬間の
        画面をそのまま残せる、(2) 余計なadb往復を増やしてポーリングを
        遅くしない、の2点のため"""
        try:
            recipe_dir = core.recipe_dir(self.name)
            img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
            recorded_pos = None
            if popup.get("x") is not None and popup.get("y") is not None:
                recorded_pos = (popup["x"], popup["y"])
            img = core.annotate_diagnostic(
                img, popup["_gray"].shape,
                best_loc=(pmethod, pval, pcx, pcy),
                recorded_pos=recorded_pos,
                tapped_pos=(tapped_x, tapped_y))
            # isalnum()だけだと日本語などの非ASCII文字も"英数字"として素通り
            # してしまい、その結果ファイル名に日本語が残る(cv2.imread/imwriteが
            # Windowsで日本語パスを静かに読み書き失敗する原因になる。core.imread/
            # imwriteで読み書き自体は保護しているが、ファイル名自体もASCIIに
            # 揃えておく方が他のツールとの互換性含めて安全なため、isascii()も
            # あわせて確認する
            safe_label = "".join(
                c if (c.isalnum() and c.isascii()) else "_" for c in popup["label"])[:30]
            fname = (f"popup_repeat_{datetime.datetime.now():%H%M%S}_"
                     f"{safe_label}_{count}.png")
            core.imwrite(recipe_dir / fname, img)
            attempts = [{"method": pmethod, "score": round(float(pval), 4),
                         "threshold": round(float(pthr), 4)}]
            core.append_popup_repeat(
                self.name, self._current_cycle, popup["label"], count,
                waiting_step_label, fname, attempts=attempts)
        except Exception as e2:
            self._log(f"!! 繰り返しポップアップの診断保存に失敗: {e2}")

    def _maybe_dismiss_popup(self, gray, popups, waiting_step_label):
        """待機ループ中の共通ポップアップ探索を間引いて呼ぶ(POPUP_CHECK_INTERVAL秒に1回)。
        タップ直後の「効いたか確認」時は_dismiss_popup_if_anyを直接呼ぶこと
        (頻度が低くタップのたびなので間引く必要が薄く、割り込み検知の
        取りこぼしを避けたいため)"""
        now = time.time()
        if now - self._last_popup_check < self.POPUP_CHECK_INTERVAL:
            return False
        self._last_popup_check = now
        return self._dismiss_popup_if_any(gray, popups, waiting_step_label)

    def _wait_and_tap(self, d, cursor, popups):
        """cursor.current() が指すノードの画像が現れるまで待ってタップする。

        cursor.is_at_start()(周回の最初のステップ)でない場合に限り、
        見つからない間、今いるブロック内で"これより後(SKIP_SEARCH_RANGE
        ステップ以内)"の兄弟ノードの画像が写っていないかも探す(他の
        ブロック、たとえばif/loopで枝分かれした別の枝へは絶対にまたがない)。
        見つかればそれをタップし、再生位置をそこまで進める。
        周回の最初のステップで見つからない場合はスキップしない。
        周回の開始画面に居ないのは想定外の状態であり、飛び先を推測すると
        全く無関係な場所に飛んで周回そのものが壊れるため(実機ログで
        5ステップ中ステップ1→5に飛ぶ事例を確認)。

        成功時、cursorを1つ(通常のタップ)または複数(スキップして復帰した
        場合)前進させたうえで戻る。戻り値: スキップして復帰したか(bool)。
        呼び出し元(run())は、この戻り値でその周を「成功」と数えるかを
        判断すること。
        タイムアウトした場合は TimeoutError を送出する。
        """
        import random
        step = cursor.current()
        # 分岐の中を実行しているときにログの階層が追えるよう、今いる
        # ブロックの深さぶんインデントする(根がdepth=1で無インデント)
        indent = "  " * (cursor.depth() - 1)
        wait_start = time.time()
        deadline = wait_start + self.step_timeout
        # best_by_method: {手法名: (これまでの最高一致度, その時のしきい値)}。
        # 試した手法すべての最高値を残しておき、タイムアウト/失敗時の
        # ヒント表示とfailures.jsonlへの記録(【実装3】)に使う
        best_by_method = {}
        self._last_attempts = None
        self._last_best_loc = None
        self._last_tapped_pos = None
        last_report = time.time()
        # このステップ待ちの間に「進んでいません」通知を出したかどうか。
        # ローカル変数なので呼び出し(=ステップ)ごとに必ずFalseへ戻り、
        # 同一ステップで2回以上通知することはない
        stall_notified = False
        while time.time() < deadline:
            if self._stop:
                raise KeyboardInterrupt
            gray = core.to_gray(d.screenshot())

            # 共通ポップアップは、対象ステップの探索より先にチェックする
            # (どのステップを待っていても、順序に関係なく割り込んで閉じる)。
            # ただしmasked_znccは重いため、間引いて探索する(POPUP_CHECK_INTERVAL)
            if self._maybe_dismiss_popup(gray, popups, step["label"]):
                continue

            cx, cy, val, method_used, thr_used, attempts = self._match_candidate(gray, step)
            for m, v, t, pcx, pcy in attempts:
                cur = best_by_method.get(m)
                if cur is None or v > cur[0]:
                    best_by_method[m] = (v, t)
                if pcx is not None and (self._last_best_loc is None
                                         or v > self._last_best_loc[1]):
                    self._last_best_loc = (m, v, pcx, pcy)
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
                    self._last_tapped_pos = (jx, jy)  # スクリーンショット空間で記録(診断用)
                    tx, ty = self._to_window(jx, jy, cur_shape)
                    core.tap(self._serial, tx, ty, self.hold_ms)
                    tag = f" [{attempt}回目]" if attempt > 1 else ""
                    self._log(
                        f"{indent}    {step['label']}: タップ({tx},{ty}) "
                        f"一致{val:.4f}[{method_used}]{fallback_note}{tag}")

                    if not self.verify:
                        time.sleep(self.after)
                        cursor.advance()
                        return False

                    # タップ後の消失確認は1回きりの判定にしない。押した直後は
                    # 画面遷移アニメーションの途中であることが多く、そこだけ
                    # 見ると「一致度が押す前より上がった(=まだ残っている)」と
                    # 誤判定して無駄な押し直しが起きる(実機ログで確認)ため、
                    # 短い間隔でポーリングして、消えた時点で即座に次へ進む。
                    # ずっと見え続けた場合にだけ「効いていない」とみなす
                    verify_start = time.time()
                    verify_limit = self.after * self.TAP_VERIFY_POLL_MAX_MULTIPLIER
                    gone = False
                    while True:
                        time.sleep(self.TAP_VERIFY_POLL_INTERVAL)
                        after_gray = core.to_gray(d.screenshot())
                        # ボタンが消えずに残っているように見えても、実は共通
                        # ポップアップに覆われていて反応していないだけ、という
                        # ケースがあるため先に確認する(頻度が低いため間引かない)
                        if self._dismiss_popup_if_any(after_gray, popups, step["label"]):
                            popup_interrupted = True
                            break
                        ncx, ncy, nval, nmethod, nthr, nattempts = self._match_candidate(
                            after_gray, step)
                        for m, v, t, pcx, pcy in nattempts:
                            cur = best_by_method.get(m)
                            if cur is None or v > cur[0]:
                                best_by_method[m] = (v, t)
                            if pcx is not None and (self._last_best_loc is None
                                                     or v > self._last_best_loc[1]):
                                self._last_best_loc = (m, v, pcx, pcy)
                        if ncx is None:
                            gone = True
                            break
                        cx, cy, method_used = ncx, ncy, nmethod
                        cur_shape = after_gray.shape
                        if time.time() - verify_start >= verify_limit:
                            break

                    if popup_interrupted:
                        break  # forループを抜けてポップアップ対応へ

                    elapsed = time.time() - verify_start
                    if gone:
                        self._log(f"{indent}    …消失確認: {elapsed:.1f}秒で消えました")
                        cursor.advance()
                        return False  # ボタンが消えた＝タップ成功、次へ

                    # まだ同じボタンが見えている＝タップが効いていない → 押し直す
                    fallback_note = "(エッジ判定で検出)" if method_used == "edge" else ""
                    self._log(
                        f"{indent}    …{elapsed:.1f}秒間ボタンが残っています"
                        f"(一致{nval:.4f}[{nmethod}])。押し直します")
                self._last_attempts = self._attempts_summary(best_by_method)
                if popup_interrupted:
                    continue  # ポップアップを閉じたので対象を探し直す
                self._log(
                    f"{indent}    !! {step['label']}: 押しても反応しません。"
                    "「タップ長押しms」を80〜150に上げてみてください")
                raise RuntimeError(
                    f"{step['label']}: {self.tap_retry}回タップしても次の画面に"
                    "遷移しませんでした(同じ場所を押しても無反応)")

            # 対象の画像が見つからない → 想定外の画面(広告・確認ダイアログ等)の
            # 可能性があるので、今いるブロック内で"これより後"の兄弟ノードの
            # 画像が写っていないか探す(前のノードや、他のブロックは対象に
            # しない)。ただし周回の最初のステップでは発動しない(上のdocstring参照)
            if not cursor.is_at_start():
                candidates = cursor.siblings_ahead(self.SKIP_SEARCH_RANGE)
                other = self._find_best_match(gray, candidates)
                if other is not None:
                    local_idx, other_step, ocx, ocy, oval, omethod, othr, oattempts = other
                    if oval < othr + self.SKIP_THRESHOLD_MARGIN:
                        # 誤って飛ぶコストが高いため、通常よりしきい値を
                        # 厳しくしている。それに届かなければスキップしない
                        other = None
                if other is not None:
                    jx = ocx + other_step.get("dx", 0) + random.randint(-self.jitter, self.jitter)
                    jy = ocy + other_step.get("dy", 0) + random.randint(-self.jitter, self.jitter)
                    tx, ty = self._to_window(jx, jy, gray.shape)
                    core.tap(self._serial, tx, ty, self.hold_ms)
                    fallback_note = "(エッジ判定で検出)" if omethod == "edge" else ""
                    # 番号ではなくラベルだけで表示する(ネストが入ると
                    # 「全体の何番目か」という単一の番号は意味を持たない。
                    # siblings_ahead()により候補は常に今のブロック内の
                    # 兄弟に限られるので、else側や親ブロックへ飛ぶことはない)
                    self._log(
                        f"{indent}    !! 「{step['label']}」をスキップして"
                        f"「{other_step['label']}」へ進みました"
                        f"(この先の画像を検知・一致{oval:.4f}[{omethod}]){fallback_note}")
                    time.sleep(self.after)
                    # 元の対象を待ち続けても二度と現れないので、進んだ先(=見つけた
                    # 兄弟ノード)の次から再開する(見つけた兄弟ノード自体はタップ
                    # 済みなので、そこはもう待たない)
                    cursor.advance_to_sibling(local_idx + 2)
                    return True

            # まだ見つからない状態がSTALL_NOTIFY_SECONDS続いたら、「各ステップ
            # 最大待ち秒」での失敗確定(最大で300秒等)を待たず、一度だけ早期に
            # 知らせる。stall_notifiedはこの呼び出し(=このステップ)専用の
            # ローカル変数なので、同一ステップで2回以上は通知しない
            if (self.notify_stall and not stall_notified
                    and time.time() - wait_start >= self.STALL_NOTIFY_SECONDS):
                stall_notified = True
                notify.notify_async(
                    "TapReplay: 進捗が止まっています",
                    f"レシピ「{self.name}」周回{self._current_cycle}\n"
                    f"ステップ「{step['label']}」が"
                    f"{self.STALL_NOTIFY_SECONDS}秒進んでいません",
                    on_error=self._log)

            # まだ見つからない → 数秒おきに現在の一致度を報告
            if time.time() - last_report >= 3:
                last_report = time.time()
                detail = " / ".join(
                    f"{m}:{v:.4f}(しきい値{t:.2f})" for m, (v, t) in best_by_method.items())
                self._log(f"{indent}    待機中… {step['label']} 最高一致度 {detail}")
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
            f"{m}:{v:.4f}(しきい値{t:.2f})" for m, (v, t) in best_by_method.items())
        raise TimeoutError(f"{step['label']} が出現せず ({detail}) {hint}")

    def _evaluate_condition(self, d, condition, indent):
        """ifノードのconditionを1回だけ判定する(_wait_and_tapのような
        ポーリング待ちはしない。その場のスクリーンショット1枚で判定する)。

        既存のcore.peak_match()をそのまま使う。しきい値を跨いだかどうか
        (found)を、condition["kind"]がimage_foundならそのまま、
        image_not_foundなら反転してthen/elseどちらを選ぶかを決める。
        戻り値: thenを選ぶか(bool)"""
        gray = core.to_gray(d.screenshot())
        method = condition.get("method", "ccoeff")
        mask = condition.get("_mask") if method == "masked_zncc" else None
        threshold = condition.get("threshold", 0.85)
        cx, cy, val = core.peak_match(gray, condition["_gray"], method=method, mask=mask)
        found = cx is not None and val >= threshold
        kind = condition["kind"]
        if kind == "image_found":
            take_then = found
        elif kind == "image_not_found":
            take_then = not found
        else:
            raise ValueError(f"未知のcondition kindです: {kind!r}")
        return take_then, val, threshold, method

    def _run_if_node(self, d, cursor, node):
        """ifノードを1つ処理する: conditionを判定し、選んだ枝(then/else)へ
        cursorを進める(enter_block)。ブロックの退出はcursor.current()側の
        自動popに任せ、run()のループがそれを検知してログに残す(この
        関数では入場だけを担当する)"""
        indent = "  " * (cursor.depth() - 1)
        label = node.get("label", "?")
        take_then, val, threshold, method = self._evaluate_condition(
            d, node["condition"], indent)
        branch = "then" if take_then else "else"
        self._log(
            f"{indent}条件判定: 「{label}」 → "
            f"一致{val:.4f}[{method}](しきい値{threshold:.2f}) → {branch} へ")
        branch_nodes = node.get(branch) or []
        self._log(f"{indent}→ 「{label}」の{branch}へ入ります({len(branch_nodes)}ノード)")
        self._block_label_stack.append((label, branch))
        cursor.enter_block(branch_nodes)

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
            # data["steps"]は木構造なので、旧形式検出・ステップ数表示などの
            # 診断目的では、ネストを問わずtapノードだけを平らにした一覧を使う
            # (if自体は"threshold"を持たないので、data["steps"]をそのまま
            # 見るとif混じりのレシピを誤って「旧形式」と判定してしまう)
            tap_nodes = list(core.iter_tap_leaves(data["steps"]))
            # ステップごとのしきい値("threshold"キー)を持たない旧形式のレシピが
            # 1件でもあれば、GUIの調整欄の値域・初期値が変わっていることに
            # よるユーザーの混乱を避けるため、その旨を1行ログに出しておく
            if any("threshold" not in s for s in tap_nodes + popups):
                self._log(
                    "!! 注意: ステップごとのしきい値を持たない旧形式のレシピです。"
                    f"「一致しきい値の調整（全体）」欄の値({self.threshold_offset:.2f})"
                    "がそのまま一致しきい値として使われます。この欄は値域・初期値が"
                    "変わっているので、検出しない/誤検出する場合は値を調整してください")
            # 一致度は-1〜1に収まるはずなので、1.0を超えるthresholdは
            # masked_zncc()の桁落ち不具合(修正済み)を前提に自動算出された
            # 不正な値である可能性が高い。もう再現はしないが、既にそうした
            # 値で記録されてしまった既存レシピを検出して知らせる
            broken_labels = [
                s.get("label", "?") for s in tap_nodes + popups
                if s.get("threshold") is not None and s["threshold"] > 1.0
            ]
            if broken_labels:
                self._log(
                    "!! 注意: 以下のステップは不正なしきい値(1.0超)で記録されて"
                    "います(過去の数値不具合が原因の可能性)。正しく検出できない"
                    f"ため撮り直しをおすすめします: {', '.join(broken_labels)}")
            # if の条件と共通ポップアップが同じ画像(テンプレートファイル)を
            # 使っている場合、共通ポップアップの割り込み処理が条件判定より
            # 先に画面を閉じてしまい、その条件が常に不成立になりかねない
            # (ConditionPickerDialogでの登録解除確認をすり抜けた場合の
            # 保険として、再生開始時にも検出しておく)
            popup_by_template = {p["template"]: p for p in popups if p.get("template")}
            for if_node, cond in core.iter_if_conditions(data["steps"]):
                tpl = cond.get("template")
                if tpl and tpl in popup_by_template:
                    self._log(
                        f"!! 注意: 分岐「{if_node.get('label', '?')}」の条件は、"
                        f"共通ポップアップ「{popup_by_template[tpl].get('label', '?')}」"
                        f"と同じ画像({tpl})を使っています。共通ポップアップの"
                        "割り込み処理が先に画面を閉じてしまうため、この条件は"
                        "常に不成立になる可能性があります(共通ポップアップの登録を"
                        "外すか、別の画像を条件にしてください)")
            self._log(f"再生開始: {len(tap_nodes)}ステップ"
                      f"（共通ポップアップ{len(popups)}件） / "
                      f"{'無限' if self.loops == 0 else self.loops}周")

            ok = ng_total = ng_streak = cycle = incomplete = 0
            stopped_by_failure = False
            started = time.time()
            while self.loops == 0 or cycle < self.loops:
                if self._stop:
                    break
                cycle += 1
                self._current_cycle = cycle
                # 共通ポップアップの繰り返し検知カウントは周回をまたいで
                # 引き継がない(周回ごとに0から数え直す)
                self._popup_repeat_counts = {}
                self._popup_repeat_saved = {}
                # 実行トレースのログ用: 今どのifの、どちらの枝に入っているかの
                # スタック(要素は(ifのlabel, "then"/"else"))。周回ごとに空へ
                self._block_label_stack = []
                self._log(f"=== ループ {cycle} ===")
                current_step = None
                try:
                    # 周回のたびに、木構造の根からたどり直す新しいカーソルを
                    # 作る(前回の走査位置を引き継がない)。事前に全ノードを
                    # 1本の配列へ展開するのではなく、1ノードずつ実行しながら
                    # 次を決める(if/loopの条件はcondition評価が実行時の
                    # 画面依存なので、事前展開できないため。core.Cursor参照)
                    cursor = core.Cursor(data["steps"])
                    skipped_this_cycle = False
                    while True:
                        current_step = cursor.current()
                        # current()の中でブロックを1つ以上抜けていたら
                        # (自動でスタックが縮んでいたら)、実行トレースの
                        # ログにその分を残す。ブロックの深さ(depth-1)より
                        # ラベルスタックが深い分だけ、実際に抜けたとみなす
                        while len(self._block_label_stack) > max(cursor.depth() - 1, 0):
                            label, branch = self._block_label_stack.pop()
                            exit_indent = "  " * len(self._block_label_stack)
                            self._log(f"{exit_indent}← 「{label}」の{branch}を抜けました")
                        if current_step is None:
                            break
                        ntype = current_step.get("type", "tap")
                        if ntype == "tap":
                            skipped = self._wait_and_tap(d, cursor, popups)
                            skipped_this_cycle = skipped_this_cycle or skipped
                        elif ntype == "if":
                            self._run_if_node(d, cursor, current_step)
                        elif ntype == "loop":
                            raise NotImplementedError(
                                "type='loop' の実行制御はフェーズ2以降で対応します")
                        else:
                            raise ValueError(f"未知のノードtypeです: {ntype!r}")
                    avg = (time.time() - started) / cycle
                    elapsed = time.time() - started
                    if skipped_this_cycle:
                        # スキップで途中のステップを飛ばした周は、実際には
                        # 手順通りに動いたか確認できていないため「成功」に
                        # 数えない(実機ログで、スキップにより5ステップ中
                        # 1ステップしか実行していない周が「成功」として
                        # 集計されてしまった事例を確認したため)
                        incomplete += 1
                        self.sig_cycle.emit(ok, ng_total, incomplete)
                        self.sig_progress.emit(cycle, self.loops, elapsed, avg)
                        self._log(
                            f"=== ループ {cycle} 完了(スキップあり・不完全扱い) "
                            f"平均 {avg:.0f}秒/回 ===")
                    else:
                        ok += 1
                        ng_streak = 0  # 連続失敗カウントは成功したらリセット
                        self.sig_cycle.emit(ok, ng_total, incomplete)
                        self.sig_progress.emit(cycle, self.loops, elapsed, avg)
                        self._log(f"=== ループ {cycle} 完了  平均 {avg:.0f}秒/回 ===")
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    ng_total += 1
                    ng_streak += 1
                    avg = (time.time() - started) / cycle
                    elapsed = time.time() - started
                    self.sig_cycle.emit(ok, ng_total, incomplete)
                    self.sig_progress.emit(cycle, self.loops, elapsed, avg)
                    self._log(f"!! ループ {cycle} 失敗: {e}")
                    # 参考情報: マスクの有効画素率と記録時に算出したしきい値。
                    # マスクがほとんど残っていないステップは、そもそも画像認識に
                    # 向いていないことが多いため、原因の見当をつけやすくする。
                    # ifノードは"_mask"/"threshold"を(あればcondition側に)
                    # 持たないため対象外(current_stepがtapのときだけ意味がある)
                    if current_step is not None and current_step.get("type", "tap") == "tap":
                        mask_arr = current_step.get("_mask")
                        if mask_arr is not None:
                            valid_ratio = float((mask_arr > 0).mean())
                            self._log(f"    参考: マスク有効画素率 {valid_ratio * 100:.1f}%")
                        ref_thr = current_step.get("threshold")
                        if ref_thr is not None:
                            self._log(f"    参考: 記録時に算出したしきい値 {ref_thr:.4f}")
                    # 失敗の記録自体が失敗しても(端末との接続切れ等)再生は止めない
                    try:
                        # isalnum()だけでは日本語などの非ASCII文字が素通りして
                        # しまう(str(e)はステップ名を含むことが多く日本語になり
                        # がち)。ファイル名はASCIIに揃えておく(core.imread/imwrite
                        # 自体はcv2のWindows日本語パス問題を回避済みだが、
                        # 他のツールとの互換性も考えて名前の方も揃えておく)
                        safe_reason = "".join(
                            c if (c.isalnum() and c.isascii()) else "_" for c in str(e))[:40]
                        fname = f"error_{datetime.datetime.now():%H%M%S}_{safe_reason}.png"
                        img = core.to_bgr(d.screenshot())
                        if current_step is not None and current_step.get("type", "tap") == "tap":
                            # 失敗時のスクショに、最も一致した位置(赤)・記録上の
                            # タップ位置(緑)・実際にタップした位置(黄)を描き込む。
                            # 「正しい場所にマッチしているのにタップが効かない」のか
                            # 「そもそも無関係な場所にマッチしている」のかを一目で
                            # 切り分けられるようにするため。ifノードの失敗(条件
                            # 判定自体の例外等)はこの位置描画の対象外(current_step
                            # 自体に"_gray"/"x"/"y"を持たないため)
                            recorded_pos = None
                            if (current_step.get("x") is not None
                                    and current_step.get("y") is not None):
                                recorded_pos = (current_step["x"], current_step["y"])
                            img = core.annotate_diagnostic(
                                img, current_step["_gray"].shape,
                                best_loc=self._last_best_loc,
                                recorded_pos=recorded_pos,
                                tapped_pos=self._last_tapped_pos)
                        core.imwrite(recipe_dir / fname, img)
                        step_label = current_step["label"] if current_step else "?"
                        core.append_failure(self.name, step_label, str(e), fname,
                                             attempts=self._last_attempts)
                    except Exception as e2:
                        self._log(f"!! 失敗時のスクリーンショット保存に失敗: {e2}")
                    if ng_streak >= self.max_fail:
                        self._log(f"!! 失敗が{ng_streak}回連続したため停止します")
                        stopped_by_failure = True
                        if self.notify_fail:
                            fail_step_label = current_step["label"] if current_step else "?"
                            notify.notify_async(
                                "TapReplay: 失敗のため停止しました",
                                f"レシピ「{self.name}」周回{cycle}\n"
                                f"ステップ「{fail_step_label}」が{ng_streak}回連続失敗\n{e}",
                                on_error=self._log)
                        break
                    # back操作自体が失敗しても(接続切れ等)スレッドを落とさず次周へ進む
                    try:
                        d.press("back")
                        # 通信が回復したので、次に切れたときまた通知できるようにする
                        self._disconnect_notified = False
                    except Exception as e2:
                        self._log(f"!! 端末との通信に失敗しました(接続切れの可能性): {e2}")
                        if self.notify_disconnect and not self._disconnect_notified:
                            self._disconnect_notified = True
                            notify.notify_async(
                                "TapReplay: 端末との接続が切れました",
                                f"レシピ「{self.name}」周回{cycle}\n{e2}",
                                on_error=self._log)
                    time.sleep(3)
                time.sleep(1)

            total = (time.time() - started) / 60
            self._log(
                f"終了: 成功{ok} / 失敗{ng_total} / 不完全{incomplete} / {total:.1f}分")
            # 指定回数の周回が完了した場合のみ通知する(0=無限ループ指定時、
            # 手動停止、失敗による停止は対象外)
            if (self.notify_done and self.loops != 0
                    and not self._stop and not stopped_by_failure):
                notify.notify_async(
                    "TapReplay: 周回が完了しました",
                    f"レシピ「{self.name}」{cycle}周 完了\n"
                    f"成功{ok} / 失敗{ng_total} / 不完全{incomplete} / {total:.1f}分",
                    on_error=self._log)
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
        self.setWindowTitle(f"TapReplay v{core.VERSION} — Android 記録＆再生")
        self.resize(560, 700)
        self.serial = None
        self.worker = None
        self._connected = False
        self._last_counts = (0, 0, 0)
        # 設定(記録・再生の各数値、端末シリアル、最後に選んだレシピ名、
        # 通知のON/OFFなど)は、recipesと同様exeのある場所を基準にした
        # settings.iniに保存する(exeフォルダごと配布・移動しても一緒に付いてくる)
        self.settings = QtCore.QSettings(
            str(core.SETTINGS_PATH), QtCore.QSettings.IniFormat)

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
            "表示されれば成功です。記録・再生の前に押さなくても、"
            "記録開始／再生開始を押した時点で未接続なら自動で接続を試みます。"))
        v.addLayout(row)

        # レシピ名 ＋ 設定リセット
        row = QtWidgets.QHBoxLayout()
        self.cmb_recipe = QtWidgets.QComboBox()
        self.cmb_recipe.setEditable(True)
        self.cmb_recipe.addItems(core.list_recipes())
        row.addWidget(help_label(
            "レシピ名:",
            "記録・再生の対象となる名前です。recipes/<この名前>/ フォルダに"
            "保存されます。新しい名前を入力すれば新規レシピとして記録できます。"))
        row.addWidget(self.cmb_recipe, 1)
        btn_reset_settings = QtWidgets.QPushButton("設定を初期値に戻す")
        btn_reset_settings.clicked.connect(self.on_reset_settings)
        row.addWidget(with_help(
            btn_reset_settings,
            "記録・再生タブの数値設定(切抜き幅/高さ、実行回数、しきい値の"
            "調整、各種待ち時間など)を初期値に戻します。設定をいじりすぎて"
            "動かなくなったときの復帰用です。レシピ名・端末シリアル・通知の"
            "ON/OFFは変更しません。"))
        v.addLayout(row)

        # タブ（記録設定 / 再生設定）
        tabs = QtWidgets.QTabWidget()

        # --- 記録タブ ---
        # よく使う操作(記録開始・分岐を編集)を上に、日常的には触らない
        # 切り抜きサイズは「詳細設定」として畳んでおく(既定で閉じた状態でも
        # ボタンは常に見えるよう、ボタンは詳細設定の外に置く)
        tab_rec = QtWidgets.QWidget()
        tv = QtWidgets.QVBoxLayout(tab_rec)
        tv.addLayout(groupbox_help(
            "端末の画面をPC上でクリックして、操作手順(レシピ)を記録します。"))

        box = QtWidgets.QGroupBox("記録")
        g = QtWidgets.QVBoxLayout(box)
        self.btn_rec = QtWidgets.QPushButton("記録開始")
        self.btn_rec.clicked.connect(self.on_record)
        g.addWidget(with_help(
            self.btn_rec,
            "上のレシピ名で記録ウィンドウを開きます。既に記録済みのレシピ名を"
            "指定した場合は、続きから追加記録するか選べます。未接続なら"
            "自動で端末への接続を試みます。"))
        self.btn_edit_structure = QtWidgets.QPushButton("分岐を編集")
        self.btn_edit_structure.clicked.connect(self.on_edit_structure)
        g.addWidget(with_help(
            self.btn_edit_structure,
            "上のレシピ名で記録済みのレシピを開き、if/elseの分岐構造を編集"
            "します。端末には接続しません(記録済みの画像を使って画面上で"
            "組み立てるだけです)。"))
        tv.addWidget(box)

        box_adv, g = make_collapsible_box("詳細設定（切り抜きサイズ）")
        self.sp_w = QtWidgets.QSpinBox(); self.sp_w.setRange(40, 800); self.sp_w.setValue(200)
        self.sp_h = QtWidgets.QSpinBox(); self.sp_h.setRange(40, 800); self.sp_h.setValue(100)
        g.addWidget(help_label(
            "切抜き幅",
            "クリックした位置を中心に、ボタン画像を切り抜く横幅(ピクセル)です。"
            "大きくすると周囲の文字ごと含められ、似たボタンと区別しやすくなります。"
            "小さすぎるとほぼ無地の画像になり、暗転画面などへの誤検知の原因になります。"
            "記録画面でクリックの代わりにドラッグすると、この値を使わずドラッグした"
            "矩形の範囲がそのまま切り抜かれます(アニメーションのある画面で、"
            "動かない部分だけを自分で選びたいときに使ってください)。"), 0, 0)
        g.addWidget(self.sp_w, 0, 1)
        g.addWidget(help_label("高さ", "切り抜く縦幅(ピクセル)です。考え方は「切抜き幅」と同じです。"), 0, 2)
        g.addWidget(self.sp_h, 0, 3)
        tv.addWidget(box_adv)
        tv.addStretch(1)
        tabs.addTab(tab_rec, "記録")

        # --- 再生タブ ---
        # 日常的に触るのは実行回数・しきい値調整くらいで、残りは不調時だけ
        # 調整する値なので「詳細設定」として畳んでおく(既定で閉じた状態)。
        # 折りたたんだ状態でも再生開始ボタンはすぐ押せるよう、ボタンは
        # 詳細設定の外に置く
        tab_play = QtWidgets.QWidget()
        pv = QtWidgets.QVBoxLayout(tab_play)
        pv.addLayout(groupbox_help("記録したレシピを自動で繰り返し実行します。"))

        box = QtWidgets.QGroupBox("再生の設定")
        g = QtWidgets.QGridLayout(box)
        self.sp_loops = QtWidgets.QSpinBox(); self.sp_loops.setRange(0, 100000); self.sp_loops.setValue(0)
        self.sp_thr_offset = QtWidgets.QDoubleSpinBox()
        self.sp_thr_offset.setRange(-0.2, 0.2)
        self.sp_thr_offset.setSingleStep(0.01)
        self.sp_thr_offset.setValue(0.0)
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
        pv.addWidget(box)

        box_adv, g = make_collapsible_box("詳細設定（普段は調整不要。うまく動かない時だけ）")
        self.sp_to = QtWidgets.QSpinBox(); self.sp_to.setRange(5, 3600); self.sp_to.setValue(300)
        self.sp_after = QtWidgets.QDoubleSpinBox(); self.sp_after.setRange(0, 20); self.sp_after.setValue(1.2)
        self.sp_poll = QtWidgets.QDoubleSpinBox(); self.sp_poll.setRange(0.3, 10); self.sp_poll.setValue(1.5)
        self.sp_fail = QtWidgets.QSpinBox(); self.sp_fail.setRange(1, 50); self.sp_fail.setValue(3)
        self.sp_hold = QtWidgets.QSpinBox(); self.sp_hold.setRange(0, 1000); self.sp_hold.setValue(0)
        self.sp_jitter = QtWidgets.QSpinBox(); self.sp_jitter.setRange(0, 100); self.sp_jitter.setValue(6)
        g.addWidget(help_label(
            "各ステップ最大待ち秒",
            "1つのステップの画像が現れるまで待つ最大時間(秒)。この時間を"
            "過ぎても見つからなければ、そのステップは失敗として扱われます。"), 0, 0)
        g.addWidget(self.sp_to, 0, 1)
        g.addWidget(help_label(
            "タップ後待ち秒",
            "ボタンをタップしてから、効いたか(消えたか)を確認するまでの"
            "待ち時間(秒)。画面の反応が遅いアプリでは長めにしてください。"), 0, 2)
        g.addWidget(self.sp_after, 0, 3)
        g.addWidget(help_label(
            "確認間隔秒",
            "対象のボタンがまだ現れていないとき、何秒おきに画面を確認しに"
            "いくかの間隔です。"), 1, 0)
        g.addWidget(self.sp_poll, 1, 1)
        g.addWidget(help_label(
            "連続失敗で停止",
            "同じレシピの再生が連続で何回失敗したら、再生全体を停止するかの"
            "回数です。途中で1回でも成功すればこのカウントはリセットされます。"), 1, 2)
        g.addWidget(self.sp_fail, 1, 3)
        g.addWidget(help_label(
            "タップ長押しms(効かない時↑)",
            "タップを押している時間(ミリ秒)。0は瞬間タップ。反応が悪い"
            "アプリではタップしても無反応になりやすいので、80〜150くらいに"
            "上げると改善することがあります。"), 2, 0)
        g.addWidget(self.sp_hold, 2, 1)
        self.ck_verify = QtWidgets.QCheckBox("タップ後に効いたか確認して押し直す")
        self.ck_verify.setChecked(True)
        g.addWidget(with_help(
            self.ck_verify,
            "ONにすると、タップ後にボタンがまだ画面に残っているか確認し、"
            "残っていれば同じ場所を押し直します。OFFにすると1回タップした"
            "だけで確認せずに次のステップへ進みます。"), 2, 2, 1, 2)
        g.addWidget(help_label(
            "タップ位置のばらつきpx",
            "タップする座標を毎回この範囲内でランダムにずらす量(ピクセル)。"
            "0にすると常に全く同じ座標をタップします。同じ場所ばかり連打する"
            "ことで一部のアプリの不正操作対策に引っかかるのを避けるためのもので、"
            "通常は初期値のままで問題ありません。"), 3, 0)
        g.addWidget(self.sp_jitter, 3, 1)
        pv.addWidget(box_adv)

        self.btn_play = QtWidgets.QPushButton("再生開始")
        self.btn_play.clicked.connect(self.on_play)
        pv.addWidget(with_help(
            self.btn_play,
            "上で選んだレシピを、この設定で再生します。未接続なら自動で"
            "端末への接続を試みます。"))

        box = QtWidgets.QGroupBox("通知(放置実行中の状況をWindowsのトースト通知でお知らせ)")
        ng = QtWidgets.QGridLayout(box)
        self.ck_notify_fail = QtWidgets.QCheckBox("失敗して停止したときに通知")
        self.ck_notify_fail.setChecked(True)
        self.ck_notify_done = QtWidgets.QCheckBox("指定回数の周回が完了したときに通知")
        self.ck_notify_done.setChecked(True)
        self.ck_notify_disconnect = QtWidgets.QCheckBox("端末との接続が切れたときに通知")
        self.ck_notify_disconnect.setChecked(True)
        self.ck_notify_stall = QtWidgets.QCheckBox("進捗が長時間止まっているときに通知")
        self.ck_notify_stall.setChecked(True)
        ng.addWidget(with_help(
            self.ck_notify_fail,
            "「連続失敗で停止」の回数だけ連続で失敗して再生そのものが"
            "停止したときに通知します。"), 0, 0)
        ng.addWidget(with_help(
            self.ck_notify_done,
            "「実行回数」で指定した周回をすべて終えたときに通知します。"
            "「実行回数」が0(無限ループ)の場合は対象外です(終わりが無いため)。"), 0, 1)
        ng.addWidget(with_help(
            self.ck_notify_disconnect,
            "再生中にUSB切断などで端末と通信できなくなったときに通知します。"
            "通信が回復すれば、次に切れたときまた通知します。"), 1, 0)
        ng.addWidget(with_help(
            self.ck_notify_stall,
            f"1つのステップの画像が{PlayerThread.STALL_NOTIFY_SECONDS}秒経っても"
            "現れないとき、「各ステップ最大待ち秒」での失敗確定を待たず、"
            "一度だけ「進んでいません」と早めに通知します。その後も待機自体は"
            "続き、最終的に失敗すれば別途「失敗して停止」の通知が出ます"
            "(同じステップで2回以上通知することはありません)。"), 1, 1)
        for cb in (self.ck_notify_fail, self.ck_notify_done,
                   self.ck_notify_disconnect, self.ck_notify_stall):
            cb.toggled.connect(self._save_notify_settings)
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

        box = QtWidgets.QGroupBox("記録したステップ")
        bl = QtWidgets.QVBoxLayout(box)
        # if/elseを含む木構造をそのまま表示するため、分岐編集画面
        # (StructureEditorDialog)と同じBlockTreeWidget/NodeTreeBuilderを
        # 読み取り専用(editable=False)で使う。並び替え・編集はできない
        self.list_steps = BlockTreeWidget()
        self.list_steps.setHeaderHidden(True)
        self.list_steps.setIconSize(QtCore.QSize(64, 64))
        self.list_steps.setDragDropMode(QtWidgets.QAbstractItemView.NoDragDrop)
        self.list_steps.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        cv.addLayout(groupbox_help(
            "このレシピで記録済みの操作ステップの一覧です。上から順番に実行されます。"
            "[if]の行は分岐(if)で、その下のthen:/else:の中に、条件が成立/不成立の"
            "ときに実行するステップがぶら下がります。"))
        bl.addWidget(self.list_steps)
        b_adjust_step = QtWidgets.QPushButton("選択したステップのマスクを調整")
        b_adjust_step.clicked.connect(
            lambda: self.on_adjust_mask("steps", self.list_steps))
        bl.addWidget(with_help(
            b_adjust_step,
            "端末に接続し直さず、保存済みの画像のまま判定範囲(マスク)を"
            "絞り込みます。アニメーションなどで判定が安定しないとき、"
            "動いていなさそうな部分だけを自分でドラッグ選択できます"
            "(絞り込むことだけができ、広げることはできません)。"))
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
        b_adjust_popup = QtWidgets.QPushButton("選択した共通ポップアップのマスクを調整")
        b_adjust_popup.clicked.connect(
            lambda: self.on_adjust_mask("popups", self.list_popups))
        bl.addWidget(with_help(
            b_adjust_popup,
            "ステップと同様、端末に接続し直さず判定範囲(マスク)を絞り込みます。"))
        b_delete_popup = QtWidgets.QPushButton("選択した共通ポップアップを削除")
        b_delete_popup.setStyleSheet("color: #b00000;")
        b_delete_popup.clicked.connect(self.on_delete_popup)
        bl.addWidget(with_help(
            b_delete_popup,
            "選んだ共通ポップアップをレシピから削除します。元に戻せません。"
            "テンプレート画像などのファイル自体は残ります。"))
        cv.addWidget(box, 1)

        # 危険な操作(レシピ丸ごと削除)は、誤クリックしにくいようタブの
        # 最下部に、区切り線を挟んで他の操作から視覚的に離して置く
        cv.addStretch(0)
        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.HLine)
        sep.setFrameShadow(QtWidgets.QFrame.Sunken)
        cv.addWidget(sep)
        btn_delete_recipe = QtWidgets.QPushButton("このレシピを削除する")
        btn_delete_recipe.setStyleSheet("color: #b00000;")
        btn_delete_recipe.clicked.connect(self.on_delete_recipe)
        cv.addWidget(with_help(
            btn_delete_recipe,
            "レシピをフォルダごと完全に削除します。記録したステップ画像・"
            "共通ポップアップ・失敗履歴もすべて消え、元に戻せません。"))

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
        # 選択時は240px高さで表示する(on_failure_selected参照)ため実際の
        # 表示はもっと大きくなるが、ここでの最小値は「何も選んでいない時の
        # プレースホルダー」用。560x700の初期ウィンドウに収まるよう控えめにする
        self.lbl_fail_preview.setMinimumSize(120, 120)
        self.lbl_fail_preview.setStyleSheet("background:#222; color:#aaa;")
        bl.addWidget(self.lbl_fail_preview, 1)
        fv.addLayout(groupbox_help(
            "過去に失敗した日時・ステップ・理由の一覧です。クリックすると、"
            "その時の端末画面のスクリーンショットを右側に表示します。"))
        fv.addWidget(box, 1)

        b_new_from_failure = QtWidgets.QPushButton("この画像から新しいステップを作る")
        b_new_from_failure.clicked.connect(self.on_new_step_from_failure)
        fv.addWidget(with_help(
            b_new_from_failure,
            "選択した失敗履歴のスクリーンショットを元に、端末に接続し直さず"
            "新しいステップ(または共通ポップアップ)を作ります。狙った場所を"
            "ドラッグで選んでください。画像に赤・緑・黄色のマーカーが"
            "描き込まれている場合があるので、マーカーにかからない範囲を"
            "選んでください。単一の静止画からの作成のため、判定範囲は"
            "全域有効(旧ccoeff相当)になります。"))

        tabs.addTab(tab_fail, "失敗履歴")

        self._load_notify_settings()
        self._load_settings()
        self._wire_settings_autosave()

        self.tabs = tabs
        tabs.currentChanged.connect(self.on_tab_changed)

        v.addWidget(tabs)

        # 接続状態・停止
        row = QtWidgets.QHBoxLayout()
        self.btn_stop = QtWidgets.QPushButton("停止")
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_stop.setEnabled(False)
        self.lbl_stat = QtWidgets.QLabel()
        row.addWidget(with_help(
            self.btn_stop,
            "実行中の記録・再生を止めます。再生中はキリの良いところで"
            "止まるまで少し時間がかかることがあります。"))
        row.addWidget(self.lbl_stat, 1)
        v.addLayout(row)
        self._set_connection_status("disconnected", "未接続")

        # 再生の進捗(周回・経過時間)。再生していない間は空欄
        row = QtWidgets.QHBoxLayout()
        self.lbl_progress = QtWidgets.QLabel()
        self.lbl_progress.setStyleSheet("font-weight: bold;")
        row.addWidget(self.lbl_progress, 1)
        self.lbl_timing = QtWidgets.QLabel()
        row.addWidget(self.lbl_timing)
        v.addLayout(row)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(8)
        v.addWidget(self.progress_bar)

        # ログ
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        v.addWidget(self.log, 1)

        self.append(f"TapReplay v{core.VERSION} 起動")

        # 起動時にadbの解決結果を必ずログへ出す。実際に接続を試みるまで
        # 気づけないと、この種の問題は切り分けに時間がかかるため
        if core.ADB_SOURCE is None:
            self.append(
                "!! adb実行ファイルが見つかりません。端末に接続できません。"
                "TapReplay.exeはdist\\TapReplayフォルダの中身(_internalフォルダ"
                "含む)を丸ごと保った状態で配布・実行してください")
            QtWidgets.QMessageBox.warning(
                self, "adbが見つかりません",
                "adb実行ファイルが見つからないため、端末に接続できません。\n\n"
                "TapReplay.exe は dist\\TapReplay フォルダの中身(_internal フォルダ"
                "含む)を丸ごと保った状態で配布・実行してください。exe単体だけを"
                "別の場所へコピーすると、この状態になります。")
        else:
            self.append(f"adb: {core.ADB} ({core.ADB_SOURCE})")
            # 起動時に一度だけ自動接続を試みる(バックグラウンドスレッドで
            # 行うため、ウィンドウの表示は妨げない)。失敗しても起動プロセス
            # 自体には影響しない(単に「未接続」のまま起動するだけ)
            self._startup_connect_thread = DeviceConnectThread(self.ed_serial.text().strip() or None)
            self._startup_connect_thread.sig_ok.connect(self._on_startup_connected)
            self._startup_connect_thread.sig_error.connect(self._on_startup_connect_failed)
            self._startup_connect_thread.start()

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
        # 起動直後、自動接続スレッド(DeviceConnectThread)がまだ動いている間に
        # ウィンドウを閉じると、実行中のQThreadを破棄することになりQtが
        # クラッシュする恐れがある。stop()できる作りではないので、ここでは
        # 完了を待つ(通常は接続試行が数秒で終わるため、実害のある待ちには
        # ならない)
        t = getattr(self, "_startup_connect_thread", None)
        if t is not None and t.isRunning():
            t.wait(5000)
        event.accept()

    def _load_notify_settings(self):
        """通知ON/OFFの設定を読み込む(既定はすべてON)"""
        for cb, key in (
            (self.ck_notify_fail, "notify/fail"),
            (self.ck_notify_done, "notify/done"),
            (self.ck_notify_disconnect, "notify/disconnect"),
            (self.ck_notify_stall, "notify/stall"),
        ):
            cb.blockSignals(True)
            cb.setChecked(self.settings.value(key, True, type=bool))
            cb.blockSignals(False)

    def _save_notify_settings(self):
        """通知ON/OFFのチェックボックスが変更されるたびに、そのまま保存する
        (「保存」操作を挟まなくても次回起動時に引き継がれるように)"""
        self.settings.setValue("notify/fail", self.ck_notify_fail.isChecked())
        self.settings.setValue("notify/done", self.ck_notify_done.isChecked())
        self.settings.setValue("notify/disconnect", self.ck_notify_disconnect.isChecked())
        self.settings.setValue("notify/stall", self.ck_notify_stall.isChecked())

    # 「設定を初期値に戻す」で戻す対象(記録・再生タブの数値設定のみ。
    # レシピ名・端末シリアル・通知ON/OFFは対象外。値は各ウィジェット
    # 生成時に指定している初期値と揃えてある)
    _RESET_DEFAULTS_SPIN = (
        # (widget属性名, settingsキー, 初期値)
        ("sp_w", "record/crop_w", 200),
        ("sp_h", "record/crop_h", 100),
        ("sp_loops", "play/loops", 0),
        ("sp_thr_offset", "play/threshold_offset", 0.0),
        ("sp_to", "play/timeout", 300),
        ("sp_after", "play/after", 1.2),
        ("sp_poll", "play/poll", 1.5),
        ("sp_fail", "play/fail_streak", 3),
        ("sp_hold", "play/hold_ms", 0),
        ("sp_jitter", "play/jitter", 6),
    )

    def _load_settings(self):
        """記録・再生タブの数値設定、端末シリアル、最後に選んでいたレシピ名を
        settings.iniから読み込んで復元する(放置周回ツールとして、起動の
        たびに設定し直さずに済むようにするため)"""
        for attr, key, default in self._RESET_DEFAULTS_SPIN:
            w = getattr(self, attr)
            w.blockSignals(True)
            value_type = float if isinstance(default, float) else int
            w.setValue(self.settings.value(key, default, type=value_type))
            w.blockSignals(False)
        self.ck_verify.blockSignals(True)
        self.ck_verify.setChecked(self.settings.value("play/verify", True, type=bool))
        self.ck_verify.blockSignals(False)
        self.ed_serial.blockSignals(True)
        self.ed_serial.setText(self.settings.value("device/serial", "", type=str))
        self.ed_serial.blockSignals(False)
        last_recipe = self.settings.value("recipe/last", "", type=str)
        if last_recipe:
            self.cmb_recipe.blockSignals(True)
            if self.cmb_recipe.findText(last_recipe) < 0:
                self.cmb_recipe.addItem(last_recipe)
            self.cmb_recipe.setCurrentText(last_recipe)
            self.cmb_recipe.blockSignals(False)

    def _wire_settings_autosave(self):
        """各設定ウィジェットが変更されるたびに、そのままsettings.iniへ保存する
        (通知ON/OFFの_save_notify_settingsと同じ方式。「保存」ボタンを
        挟まない)"""
        for attr, key, _default in self._RESET_DEFAULTS_SPIN:
            w = getattr(self, attr)
            w.valueChanged.connect(lambda v, key=key: self.settings.setValue(key, v))
        self.ck_verify.toggled.connect(
            lambda v: self.settings.setValue("play/verify", v))
        self.ed_serial.textChanged.connect(
            lambda t: self.settings.setValue("device/serial", t))
        self.cmb_recipe.currentTextChanged.connect(
            lambda t: self.settings.setValue("recipe/last", t))

    def on_reset_settings(self):
        resp = QtWidgets.QMessageBox.question(
            self, "設定を初期値に戻す",
            "記録・再生タブの数値設定(切抜き幅/高さ、実行回数、しきい値の"
            "調整、各種待ち時間など)を初期値に戻します。\n"
            "レシピ名・端末シリアル・通知のON/OFFは変更しません。\n"
            "よろしいですか？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if resp != QtWidgets.QMessageBox.Yes:
            return
        for attr, _key, default in self._RESET_DEFAULTS_SPIN:
            getattr(self, attr).setValue(default)
        self.ck_verify.setChecked(True)
        self.append("設定を初期値に戻しました")

    def _set_connection_status(self, state, text):
        """接続状態を色付きで示す。state: 'connected'(緑)/'disconnected'(灰)/
        'error'(赤)。未接続に気づかず記録・再生を試みて戸惑う、という状況を
        減らすための表示(【UI改善: 接続状態を分かりやすくする】)"""
        colors = {"connected": "#1a7a1a", "disconnected": "#777777", "error": "#b00000"}
        self._connected = (state == "connected")
        self.lbl_stat.setText(text)
        self.lbl_stat.setStyleSheet(f"color: {colors[state]}; font-weight: bold;")

    def _ensure_connected(self):
        """記録開始・再生開始が押された時点で未接続なら、エラーにする前に
        自動で接続を試みる。成功すればそのまま処理を続け、失敗した場合のみ
        呼び出し元がエラーとして扱う"""
        if self._connected:
            return True
        self.append("未接続のため、自動で端末への接続を試みます…")
        self.on_connect()
        return self._connected

    def _on_startup_connected(self, d, sw, sh, pil):
        info = d.device_info
        self._set_connection_status(
            "connected", f"接続: {info.get('model')}  {(sw, sh)}")
        self.append(f"起動時の自動接続に成功しました: {info.get('model')} / {d.serial}")

    def _on_startup_connect_failed(self, message):
        # 起動時点では端末がまだ繋がっていないことも多いので、警告ではなく
        # 情報としてログに残すだけに留める(起動自体は妨げない)
        self.append(f"起動時の自動接続はできませんでした({message})。"
                     "「接続」を押すか、記録・再生開始時に改めて自動接続を試みます")

    def on_connect(self):
        self.serial = self.ed_serial.text().strip() or None
        try:
            d = core.connect(self.serial)
            info = d.device_info
            self._set_connection_status(
                "connected", f"接続: {info.get('model')}  {d.window_size()}")
            self.append(f"接続成功: {info.get('model')} / {d.serial}")
        except Exception as e:
            self._set_connection_status("error", "接続失敗")
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
        if not self._ensure_connected():
            self.append("!! 端末に接続できないため、記録を開始できません")
            return
        try:
            # RecorderDialog()はUI構築のみで端末通信を行わないため、ここは
            # 即座に返る(端末接続は表示後にダイアログ側が非同期で行う)。
            # そのため直後の「開きました」ログは、待たされた後ではなく
            # 実際にウィンドウが表示されるタイミングに一致する
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

    def on_edit_structure(self):
        name = self.cmb_recipe.currentText().strip()
        if not name:
            self.append("!! レシピ名を入れてください")
            return
        if not is_valid_recipe_name(name):
            self.append(f"!! レシピ名に使えない文字が含まれています: {INVALID_NAME_CHARS}")
            return
        if not (core.recipe_path(name) / "recipe.json").exists():
            self.append(f"!! 「{name}」はまだ記録されていません。先に記録してください")
            return
        try:
            dlg = StructureEditorDialog(name, self)
        except Exception as e:
            self.append(f"!! 分岐編集画面の準備に失敗: {e}")
            return
        self.append(f"分岐編集画面を開きました（{name}）")
        dlg.exec()
        self.append(f"分岐編集画面を閉じました（{name}）")
        self.refresh_history()

    def on_adjust_mask(self, kind, list_widget):
        name = self.cmb_recipe.currentText().strip()
        if not name:
            self.append("!! レシピ名を入れてください")
            return
        if not is_valid_recipe_name(name):
            self.append(f"!! レシピ名に使えない文字が含まれています: {INVALID_NAME_CHARS}")
            return
        if kind == "steps":
            # stepsはif/elseを含む木構造なので、一覧上の行番号は
            # data["steps"]のインデックスとは一致しない(ifが混ざると
            # ズレて、無関係なステップを書き換えてしまう)。NodeTreeBuilder
            # がroleに積んでおいたpath(木構造上の位置)で対象を特定する
            item = list_widget.currentItem()
            role = list_widget.get_role(item) if item is not None else None
            if not role or role.get("kind") != "node":
                self.append("!! 調整するステップを一覧から選択してください"
                             "(then:/else:の見出し行は選べません)")
                return
            if role["node"].get("type") != "tap":
                self.append("!! [if]の行自体はマスク調整の対象外です"
                             "(判定条件は「分岐を編集」画面から選び直してください)")
                return
            target = role["path"]
        else:
            row = list_widget.currentRow()
            if row < 0:
                self.append("!! 調整する項目を一覧から選択してください")
                return
            target = row
        try:
            dlg = MaskEditorDialog(name, kind, target, self)
        except Exception as e:
            self.append(f"!! マスク調整画面の準備に失敗: {e}")
            return
        dlg.exec()
        self.append(f"マスク調整画面を閉じました（{name}）")
        self.refresh_history()

    def on_delete_popup(self):
        name = self.cmb_recipe.currentText().strip()
        if not name:
            self.append("!! レシピ名を入れてください")
            return
        if not is_valid_recipe_name(name):
            self.append(f"!! レシピ名に使えない文字が含まれています: {INVALID_NAME_CHARS}")
            return
        row = self.list_popups.currentRow()
        if row < 0:
            self.append("!! 削除する共通ポップアップを一覧から選択してください")
            return
        recipe_dir = core.recipe_path(name)
        try:
            data = json.loads((recipe_dir / "recipe.json").read_text(encoding="utf-8"))
        except Exception as e:
            self.append(f"!! レシピの読み込みに失敗: {e}")
            return
        popups = data.get("popups", [])
        if not (0 <= row < len(popups)):
            self.append("!! 選択が無効です(表示を更新してからやり直してください)")
            return
        label = popups[row].get("label", "?")
        resp = QtWidgets.QMessageBox.question(
            self, "削除の確認",
            f"共通ポップアップ「{label}」を削除しますか？\n"
            "元に戻せません(テンプレート画像などのファイル自体は残ります)。",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if resp != QtWidgets.QMessageBox.Yes:
            return
        del popups[row]
        core.save_recipe(name, data)
        self.append(f"共通ポップアップ「{label}」を削除しました（{name}）")
        self.refresh_history()

    def on_new_step_from_failure(self):
        name = self.cmb_recipe.currentText().strip()
        if not name:
            self.append("!! レシピ名を入れてください")
            return
        if not is_valid_recipe_name(name):
            self.append(f"!! レシピ名に使えない文字が含まれています: {INVALID_NAME_CHARS}")
            return
        item = self.list_failures.currentItem()
        fname = item.data(QtCore.Qt.UserRole) if item is not None else None
        if not fname:
            self.append("!! 元にする失敗履歴の画像を一覧から選択してください")
            return
        recipe_dir = core.recipe_path(name)
        image_path = recipe_dir / fname
        if not image_path.exists():
            self.append(f"!! 画像が見つかりません: {fname}")
            return

        try:
            result = ScreenCropDialog.pick(
                image_path, title=f"新しいステップを作成: {name}", parent=self)
        except Exception as e:
            self.append(f"!! 画像の読み込みに失敗: {e}")
            return
        if result is None:
            return
        tpl_gray, mask, cx, cy, dx, dy = result

        label, ok = QtWidgets.QInputDialog.getText(self, "ステップの名前", "名前を入力してください:")
        if not ok or not label.strip():
            return
        resp = QtWidgets.QMessageBox.question(
            self, "追加先",
            "共通ポップアップとして追加しますか？\n"
            "「はい」: 共通ポップアップ(順序を問わず割り込みを閉じる)\n"
            "「いいえ」: 通常のステップ(手順の末尾に追加)",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        as_popup = resp == QtWidgets.QMessageBox.Yes

        data = json.loads((recipe_dir / "recipe.json").read_text(encoding="utf-8"))
        data.setdefault("steps", [])
        data.setdefault("popups", [])
        ts = datetime.datetime.now().strftime("%H%M%S")
        prefix = "popup_from_failure" if as_popup else "step_from_failure"
        tpl_name = f"{prefix}_{ts}.png"
        mask_name = f"{prefix}_{ts}_mask.png"
        core.imwrite(recipe_dir / tpl_name, tpl_gray)
        core.imwrite(recipe_dir / mask_name, mask)
        new_node = {
            "type": "tap",
            "label": label.strip(),
            "template": tpl_name,
            "mask": mask_name,
            "context": fname,  # 元の失敗履歴画像をそのまま参考画像として使う
            "x": cx, "y": cy, "dx": dx, "dy": dy,
            "method": "masked_zncc",
            "threshold": 0.85,
        }
        (data["popups"] if as_popup else data["steps"]).append(new_node)
        core.save_recipe(name, data)
        kind_label = "共通ポップアップ" if as_popup else "ステップ"
        self.append(f"失敗履歴の画像から新しい{kind_label}「{label.strip()}」を追加しました（{name}）")
        self.refresh_history()

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
        if not self._ensure_connected():
            self.append("!! 端末に接続できないため、再生を開始できません")
            return
        loops = self.sp_loops.value()
        self.worker = PlayerThread(
            self.serial, name,
            loops, self.sp_thr_offset.value(),
            self.sp_to.value(), self.sp_after.value(),
            self.sp_poll.value(), self.sp_jitter.value(), self.sp_fail.value(),
            verify=self.ck_verify.isChecked(), tap_retry=3,
            hold_ms=self.sp_hold.value(),
            notify_fail=self.ck_notify_fail.isChecked(),
            notify_done=self.ck_notify_done.isChecked(),
            notify_disconnect=self.ck_notify_disconnect.isChecked(),
            notify_stall=self.ck_notify_stall.isChecked(),
        )
        self._last_counts = (0, 0, 0)
        self.lbl_progress.setText(f"0 / {loops} 周目" if loops else "開始しています…")
        self.lbl_timing.setText("")
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(bool(loops))
        if loops:
            self.progress_bar.setRange(0, loops)
        self.worker.sig_log.connect(self.append)
        self.worker.sig_cycle.connect(self.on_cycle_update)
        self.worker.sig_progress.connect(self.on_progress_update)
        self.worker.sig_done.connect(self.on_worker_done)
        self._busy(True)
        self.worker.start()

    def on_cycle_update(self, ok, ng, incomplete):
        # sig_progressと対になって同じ瞬間に発火するので、ここでは値を
        # 覚えておくだけにし、実際の表示更新はon_progress_updateで行う
        self._last_counts = (ok, ng, incomplete)

    def on_progress_update(self, cycle, loops, elapsed, avg):
        """周回の進捗(今何周目か・経過時間・1周あたりの平均時間)を表示する。
        値はPlayerThreadが既にログ用に算出しているものをそのまま使う"""
        ok, ng, incomplete = self._last_counts
        text = f"{cycle} / {loops} 周目" if loops else f"{cycle} 周目"
        text += f"（成功{ok} / 失敗{ng} / 不完全{incomplete}）"
        self.lbl_progress.setText(text)
        self.lbl_timing.setText(
            f"経過 {format_duration(elapsed)} ・ 平均 {format_duration(avg)}/周")
        if loops:
            self.progress_bar.setVisible(True)
            self.progress_bar.setRange(0, loops)
            self.progress_bar.setValue(min(cycle, loops))
        else:
            self.progress_bar.setVisible(False)

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
        self.list_steps.reset_roles()
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

            # steps は if/else を含む木構造なので、分岐編集画面と同じ
            # NodeTreeBuilder で(読み取り専用として)ツリー表示する。popups は
            # 常にフラットな配列のままなので、従来通りサムネイル付きの
            # 単純な一覧のままでよい
            NodeTreeBuilder(self.list_steps, d, editable=False).build(data.get("steps", []))
            for i, p in enumerate(data.get("popups", []), 1):
                ctx_name = p.get("context") or f"context_popup_{i:02d}.png"
                add_thumb_item(self.list_popups, f"P{i}. {p.get('label', '?')}",
                               d / ctx_name)
            if not data.get("popups"):
                self.list_popups.addItem("(共通ポップアップは未登録です)")
        else:
            self.list_steps.addTopLevelItem(
                QtWidgets.QTreeWidgetItem(["(このレシピはまだ記録されていません)"]))

        failures = core.load_failures(name)
        # 「よく止まる箇所」のランキングは、従来通り実際の失敗(タイムアウト・
        # 押しても反応しない等)だけを対象にする。繰り返しポップアップの記録は
        # 失敗ではない(検知・クローズ自体は毎回成功している)ため、ここに
        # 混ぜるとランキングの意味が変わってしまうので除外する
        counts = {}
        for f in failures:
            if f.get("kind") == "popup_repeat":
                continue
            label = f.get("step_label", "?")
            counts[label] = counts.get(label, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        self.tbl_rank.setRowCount(len(ranked))
        for row, (label, cnt) in enumerate(ranked):
            self.tbl_rank.setItem(row, 0, QtWidgets.QTableWidgetItem(label))
            self.tbl_rank.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{cnt}回"))

        for f in failures[:100]:
            if f.get("kind") == "popup_repeat":
                # 通常の失敗と見分けられるよう、先頭に区別用のタグを付ける
                text = (f"{f.get('ts', '?')}  [繰り返しポップアップ] "
                        f"{f.get('popup_label', '?')} この周で{f.get('count', '?')}回目"
                        f"  待機中:「{f.get('waiting_step_label', '?')}」")
            else:
                text = f"{f.get('ts', '?')}  {f.get('step_label', '?')}  {f.get('reason', '')[:30]}"
            # attempts: そのステップの検出で試した手法ごとの最高一致度
            # (【実装3】)。どの手法が実際に効いているかを一覧で分かるようにする
            attempts = f.get("attempts")
            if attempts:
                summary = " / ".join(
                    f"{a.get('method', '?')}:{a.get('score', 0):.4f}" for a in attempts)
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
