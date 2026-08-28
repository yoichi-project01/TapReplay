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

        self.refresh()

    def _msg(self, m):
        self.log.appendPlainText(m)

    def refresh(self):
        try:
            self.pil = self.d.screenshot()
        except Exception as e:
            self._msg(f"!! スクショ失敗: {e}")
            return
        maxh = 680
        self.scale = min(1.0, maxh / self.sh)
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
        cv2.imwrite(str(self.dir / f"context_{idx:02d}.png"), ctx)
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

    def stop(self):
        self._stop = True

    def _wait_and_tap(self, d, step):
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
                    self.sig_log.emit(
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
                    self.sig_log.emit(
                        f"    …まだボタンが残っています(一致{nval:.2f})。押し直します")
                self.sig_log.emit(
                    f"    !! {step['label']}: 押しても反応しません。"
                    "「タップ長押しms」を80〜150に上げてみてください")
                return

            # まだ見つからない → 数秒おきに現在の一致度を報告
            if time.time() - last_report >= 3:
                last_report = time.time()
                self.sig_log.emit(
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
        try:
            d = core.connect(self.serial)
            self._serial = self.serial or d.serial
            data = core.load_recipe(self.name)
            sw, sh = d.window_size()
            if data.get("device_size") and list(data["device_size"]) != [sw, sh]:
                self.sig_log.emit(
                    f"!! 注意: 記録時({data['device_size']})と画面サイズが違います。"
                    "解像度・向きを合わせてください"
                )
            self.sig_log.emit(f"再生開始: {len(data['steps'])}ステップ / "
                              f"{'無限' if self.loops == 0 else self.loops}周")

            ok = ng = cycle = 0
            started = time.time()
            while self.loops == 0 or cycle < self.loops:
                if self._stop:
                    break
                cycle += 1
                self.sig_log.emit(f"=== ループ {cycle} ===")
                try:
                    for step in data["steps"]:
                        self._wait_and_tap(d, step)
                    ok += 1
                    self.sig_cycle.emit(ok, ng)
                    avg = (time.time() - started) / cycle
                    self.sig_log.emit(f"=== ループ {cycle} 完了  平均 {avg:.0f}秒/回 ===")
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    ng += 1
                    self.sig_cycle.emit(ok, ng)
                    self.sig_log.emit(f"!! ループ {cycle} 失敗: {e}")
                    cv2.imwrite(
                        str(core.recipe_dir(self.name) /
                            f"error_{datetime.datetime.now():%H%M%S}.png"),
                        core.to_gray(d.screenshot())
                    )
                    if ng >= self.max_fail:
                        self.sig_log.emit("!! 失敗が続くため停止します")
                        break
                    d.press("back")
                    time.sleep(3)
                time.sleep(1)

            total = (time.time() - started) / 60
            self.sig_log.emit(f"終了: 成功{ok} / 失敗{ng} / {total:.1f}分")
        except Exception as e:
            self.sig_log.emit(f"!! 再生エラー: {e}")
        finally:
            self.sig_done.emit()


# ================================================================== 画面
class MainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TapReplay — Android 記録＆再生")
        self.resize(560, 640)
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
        self.cmb_recipe.setCurrentText("sample")
        row.addWidget(QtWidgets.QLabel("レシピ名:"))
        row.addWidget(self.cmb_recipe, 1)
        v.addLayout(row)

        # 記録設定
        box = QtWidgets.QGroupBox("記録の設定（画面をクリックして記録）")
        g = QtWidgets.QGridLayout(box)
        self.sp_w = QtWidgets.QSpinBox(); self.sp_w.setRange(40, 800); self.sp_w.setValue(200)
        self.sp_h = QtWidgets.QSpinBox(); self.sp_h.setRange(40, 800); self.sp_h.setValue(100)
        g.addWidget(QtWidgets.QLabel("切抜き幅"), 0, 0); g.addWidget(self.sp_w, 0, 1)
        g.addWidget(QtWidgets.QLabel("高さ"), 0, 2); g.addWidget(self.sp_h, 0, 3)
        self.btn_rec = QtWidgets.QPushButton("記録開始")
        self.btn_rec.clicked.connect(self.on_record)
        g.addWidget(self.btn_rec, 1, 0, 1, 4)
        v.addWidget(box)

        # 再生設定
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
        v.addWidget(box)

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


if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    w = MainWindow()
    w.show()
    app.exec()
