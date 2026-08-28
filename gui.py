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
import datetime

import cv2
import numpy as np
from PySide6 import QtWidgets, QtCore, QtGui

import core


def pil_to_qpix(pil_img):
    buf = io.BytesIO()
    pil_img.save(buf, "PNG")
    qimg = QtGui.QImage.fromData(buf.getvalue(), "PNG")
    return QtGui.QPixmap.fromImage(qimg)


# ============================================ クリックできる画像ラベル
class ClickableLabel(QtWidgets.QLabel):
    clicked = QtCore.Signal(int, int)

    def mousePressEvent(self, e):
        self.clicked.emit(int(e.position().x()), int(e.position().y()))


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
        side.addWidget(self.ck_send)

        self.sp_delay = QtWidgets.QDoubleSpinBox()
        self.sp_delay.setRange(0.3, 10); self.sp_delay.setValue(1.5)
        drow = QtWidgets.QHBoxLayout()
        drow.addWidget(QtWidgets.QLabel("送信後に画面更新するまで秒"))
        drow.addWidget(self.sp_delay)
        side.addLayout(drow)

        b_refresh = QtWidgets.QPushButton("画面更新")
        b_refresh.clicked.connect(self.refresh)
        b_undo = QtWidgets.QPushButton("一つ戻す")
        b_undo.clicked.connect(self.undo)
        b_save = QtWidgets.QPushButton("保存して閉じる")
        b_save.clicked.connect(self.save)
        b_cancel = QtWidgets.QPushButton("キャンセル")
        b_cancel.clicked.connect(self.reject)
        for b in (b_refresh, b_undo, b_save, b_cancel):
            side.addWidget(b)

        self.log = QtWidgets.QPlainTextEdit(); self.log.setReadOnly(True)
        side.addWidget(self.log, 1)
        root.addLayout(side)

        self._load_existing()
        self.refresh()

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
        if not prev_steps:
            return

        resp = QtWidgets.QMessageBox.question(
            self, "既存レシピが見つかりました",
            f"「{self.name}」には既に{len(prev_steps)}ステップ記録されています。\n\n"
            "「はい」: プログラムが止まった続きから追加記録する\n"
            "「いいえ」: 最初からやり直す（既存の記録は削除されます）",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        )
        if resp == QtWidgets.QMessageBox.Yes:
            self.steps = prev_steps
            self._msg(f"続きから記録します（{len(prev_steps)}ステップ目まで読み込み済み）")
            dev_size = data.get("device_size")
            if dev_size and list(dev_size) != [self.sw, self.sh]:
                self._msg(
                    f"!! 注意: 記録時({dev_size})と今の画面サイズ"
                    f"({self.sw},{self.sh})が違います")
        else:
            for f in list(self.dir.glob("step_*.png")) + list(self.dir.glob("context_*.png")):
                try:
                    f.unlink()
                except Exception:
                    pass
            self._msg("既存の記録を削除し、最初から記録します")

    def _msg(self, m):
        self.log.appendPlainText(m)

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
        # 記録済みの位置に番号入りマーカーを描く
        painter = QtGui.QPainter(pix)
        pen = QtGui.QPen(QtGui.QColor(255, 0, 0)); pen.setWidth(3)
        painter.setPen(pen)
        painter.setFont(QtGui.QFont("Arial", 14, QtGui.QFont.Bold))
        for i, s in enumerate(self.steps, 1):
            x = int(s["x"] * self.scale); y = int(s["y"] * self.scale)
            painter.drawEllipse(QtCore.QPoint(x, y), 14, 14)
            painter.drawText(x + 16, y + 6, str(i))
        painter.end()
        self.img.setPixmap(pix)
        self.img.setFixedSize(pix.size())
        # 更新が終わったのでクリックを再度受け付ける
        self.img.setEnabled(True)

    def on_click(self, lx, ly):
        if self.pil is None:
            return
        rx = int(lx / self.scale)
        ry = int(ly / self.scale)
        idx = len(self.steps) + 1
        tpl = f"step_{idx:02d}.png"
        core.crop(self.pil, rx, ry, self.tpl_w, self.tpl_h).save(self.dir / tpl)
        ctx = cv2.cvtColor(np.array(self.pil), cv2.COLOR_RGB2BGR)
        cv2.rectangle(ctx,
                      (rx - self.tpl_w // 2, ry - self.tpl_h // 2),
                      (rx + self.tpl_w // 2, ry + self.tpl_h // 2),
                      (0, 0, 255), 4)
        core.imwrite(self.dir / f"context_{idx:02d}.png", ctx)
        self.steps.append({"label": f"タップ{idx}", "template": tpl, "x": rx, "y": ry})
        self._msg(f"step{idx}: ({rx},{ry}) → {tpl}")

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
        if not self.steps:
            return
        s = self.steps.pop()
        idx = len(self.steps) + 1
        for f in (self.dir / s["template"], self.dir / f"context_{idx:02d}.png"):
            try:
                f.unlink()
            except Exception:
                pass
        self._msg(f"step{idx} を取り消しました")
        self.refresh()

    def save(self):
        if not self.steps:
            self._msg("!! 1つもクリックされていません")
            return
        core.save_recipe(self.name, {
            "device_size": [self.sw, self.sh],
            "steps": self.steps,
        })
        self._msg(f"保存しました: recipes/{self.name}/ ({len(self.steps)}ステップ)")
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

    def _find_other_match(self, gray, current_step, all_steps):
        """現在待っている画像以外に、レシピ内の別のステップ画像が
        今の画面に写っていないか探す（想定外のポップアップ等からの復帰用）"""
        best = None
        for s in all_steps:
            if s is current_step:
                continue
            cx, cy, val = core.match(gray, s["_gray"], self.threshold)
            if cx is not None and (best is None or val > best[3]):
                best = (s, cx, cy, val)
        return best

    def _wait_and_tap(self, d, step, all_steps):
        import random
        deadline = time.time() + self.step_timeout
        best = 0.0
        last_report = time.time()
        while time.time() < deadline:
            if self._stop:
                raise KeyboardInterrupt
            gray = core.to_gray(d.screenshot())
            cx, cy, val = core.match(gray, step["_gray"], self.threshold)
            best = max(best, val)

            if cx is not None:
                # 見つかった → タップ。効かなければ押し直す。
                # 成功判定は「押したボタンが画面から消えたか」で見る
                # （背景アニメに惑わされない）
                for attempt in range(1, self.tap_retry + 1):
                    jx = cx + random.randint(-self.jitter, self.jitter)
                    jy = cy + random.randint(-self.jitter, self.jitter)
                    core.tap(self._serial, jx, jy, self.hold_ms)
                    tag = f" [{attempt}回目]" if attempt > 1 else ""
                    self._log(
                        f"    {step['label']}: タップ({jx},{jy}) 一致{val:.2f}{tag}")
                    time.sleep(self.after)
                    if not self.verify:
                        return
                    after_gray = core.to_gray(d.screenshot())
                    ncx, ncy, nval = core.match(after_gray, step["_gray"], self.threshold)
                    if ncx is None:
                        return  # ボタンが消えた＝タップ成功、次へ
                    # まだ同じボタンが見えている＝タップが効いていない → 押し直す
                    cx, cy = ncx, ncy
                    self._log(
                        f"    …まだボタンが残っています(一致{nval:.2f})。押し直します")
                self._log(
                    f"    !! {step['label']}: 押しても反応しません。"
                    "「タップ長押しms」を80〜150に上げてみてください")
                raise RuntimeError(
                    f"{step['label']}: {self.tap_retry}回タップしても次の画面に"
                    "遷移しませんでした(同じ場所を押しても無反応)")

            # 対象の画像が見つからない → 想定外の画面(広告・確認ダイアログ等)の
            # 可能性があるので、レシピ内の他のステップ画像が写っていないか探す
            other = self._find_other_match(gray, step, all_steps)
            if other is not None:
                other_step, ocx, ocy, oval = other
                jx = ocx + random.randint(-self.jitter, self.jitter)
                jy = ocy + random.randint(-self.jitter, self.jitter)
                core.tap(self._serial, jx, jy, self.hold_ms)
                self._log(
                    f"    !! {step['label']} は見つかりませんが、レシピ内の別画像"
                    f"「{other_step['label']}」を検知(一致{oval:.2f})したのでタップしました")
                time.sleep(self.after)
                continue

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
            self._log(f"再生開始: {len(data['steps'])}ステップ / "
                      f"{'無限' if self.loops == 0 else self.loops}周")

            ok = ng = cycle = 0
            started = time.time()
            while self.loops == 0 or cycle < self.loops:
                if self._stop:
                    break
                cycle += 1
                self._log(f"=== ループ {cycle} ===")
                current_step = None
                try:
                    for step in data["steps"]:
                        current_step = step
                        self._wait_and_tap(d, step, data["steps"])
                    ok += 1
                    self.sig_cycle.emit(ok, ng)
                    avg = (time.time() - started) / cycle
                    self._log(f"=== ループ {cycle} 完了  平均 {avg:.0f}秒/回 ===")
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    ng += 1
                    self.sig_cycle.emit(ok, ng)
                    self._log(f"!! ループ {cycle} 失敗: {e}")
                    safe_reason = "".join(
                        c if c.isalnum() else "_" for c in str(e))[:40]
                    fname = f"error_{datetime.datetime.now():%H%M%S}_{safe_reason}.png"
                    core.imwrite(recipe_dir / fname, core.to_gray(d.screenshot()))
                    step_label = current_step["label"] if current_step else "?"
                    core.append_failure(self.name, step_label, str(e), fname)
                    if ng >= self.max_fail:
                        self._log("!! 失敗が続くため停止します")
                        break
                    d.press("back")
                    time.sleep(3)
                time.sleep(1)

            total = (time.time() - started) / 60
            self._log(f"終了: 成功{ok} / 失敗{ng} / {total:.1f}分")
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
        row.addWidget(self.btn_conn)
        v.addLayout(row)

        # レシピ名
        row = QtWidgets.QHBoxLayout()
        self.cmb_recipe = QtWidgets.QComboBox()
        self.cmb_recipe.setEditable(True)
        self.cmb_recipe.addItems(core.list_recipes())
        row.addWidget(QtWidgets.QLabel("レシピ名:"))
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
        g.addWidget(QtWidgets.QLabel("切抜き幅"), 0, 0); g.addWidget(self.sp_w, 0, 1)
        g.addWidget(QtWidgets.QLabel("高さ"), 0, 2); g.addWidget(self.sp_h, 0, 3)
        self.btn_rec = QtWidgets.QPushButton("記録開始")
        self.btn_rec.clicked.connect(self.on_record)
        g.addWidget(self.btn_rec, 1, 0, 1, 4)
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
        g.addWidget(QtWidgets.QLabel("実行回数(0=無限)"), 0, 0); g.addWidget(self.sp_loops, 0, 1)
        g.addWidget(QtWidgets.QLabel("一致しきい値"), 0, 2); g.addWidget(self.sp_thr, 0, 3)
        g.addWidget(QtWidgets.QLabel("各ステップ最大待ち秒"), 1, 0); g.addWidget(self.sp_to, 1, 1)
        g.addWidget(QtWidgets.QLabel("タップ後待ち秒"), 1, 2); g.addWidget(self.sp_after, 1, 3)
        g.addWidget(QtWidgets.QLabel("確認間隔秒"), 2, 0); g.addWidget(self.sp_poll, 2, 1)
        g.addWidget(QtWidgets.QLabel("連続失敗で停止"), 2, 2); g.addWidget(self.sp_fail, 2, 3)
        g.addWidget(QtWidgets.QLabel("タップ長押しms(効かない時↑)"), 3, 0); g.addWidget(self.sp_hold, 3, 1)
        self.ck_verify = QtWidgets.QCheckBox("タップ後に効いたか確認して押し直す")
        self.ck_verify.setChecked(True)
        g.addWidget(self.ck_verify, 3, 2, 1, 2)
        self.btn_play = QtWidgets.QPushButton("再生開始")
        self.btn_play.clicked.connect(self.on_play)
        g.addWidget(self.btn_play, 4, 0, 1, 4)
        pv.addWidget(box)
        pv.addStretch(1)
        tabs.addTab(tab_play, "再生")

        # --- 確認タブ（記録内容・よく止まる箇所） ---
        tab_hist = QtWidgets.QWidget()
        hv = QtWidgets.QVBoxLayout(tab_hist)

        btn_refresh_hist = QtWidgets.QPushButton("表示を更新（上のレシピ名を対象）")
        btn_refresh_hist.clicked.connect(self.refresh_history)
        hv.addWidget(btn_refresh_hist)

        box = QtWidgets.QGroupBox("記録したステップ")
        bl = QtWidgets.QVBoxLayout(box)
        self.list_steps = QtWidgets.QListWidget()
        self.list_steps.setIconSize(QtCore.QSize(64, 64))
        self.list_steps.setMaximumHeight(160)
        bl.addWidget(self.list_steps)
        hv.addWidget(box)

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
        hv.addWidget(box)

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
        hv.addWidget(box, 1)

        tabs.addTab(tab_hist, "確認")
        self.tabs = tabs
        tabs.currentChanged.connect(self.on_tab_changed)

        v.addWidget(tabs)

        # 停止・状態
        row = QtWidgets.QHBoxLayout()
        self.btn_stop = QtWidgets.QPushButton("停止")
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_stop.setEnabled(False)
        self.lbl_stat = QtWidgets.QLabel("未接続")
        row.addWidget(self.btn_stop)
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
        if self.tabs.tabText(index) == "確認":
            self.refresh_history()

    def refresh_history(self):
        name = self.cmb_recipe.currentText().strip()
        self.list_steps.clear()
        self.tbl_rank.setRowCount(0)
        self.list_failures.clear()
        self.lbl_fail_preview.setPixmap(QtGui.QPixmap())
        self.lbl_fail_preview.setText("失敗履歴をクリックすると\nここに画像が表示されます")
        if not name:
            return

        d = core.recipe_dir(name)
        recipe_file = d / "recipe.json"
        if recipe_file.exists():
            try:
                data = json.loads(recipe_file.read_text(encoding="utf-8"))
            except Exception as e:
                data = {"steps": []}
                self.append(f"!! レシピの読み込みに失敗: {e}")
            for i, s in enumerate(data.get("steps", []), 1):
                item = QtWidgets.QListWidgetItem(f"{i}. {s.get('label', '?')}")
                ctx = d / f"context_{i:02d}.png"
                if ctx.exists():
                    item.setIcon(QtGui.QIcon(str(ctx)))
                self.list_steps.addItem(item)
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
        path = core.recipe_dir(name) / fname
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
