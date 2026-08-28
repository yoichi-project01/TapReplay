"""
core.py  ―  端末接続・タッチ記録・画像マッチングの共通処理
============================================================
GUI (gui.py) から呼ばれる。単体では起動しない。

依存:
    pip install uiautomator2 opencv-python numpy pillow
前提:
    Windows に platform-tools (adb.exe) を入れて PATH を通す
    端末: 開発者オプション → USB デバッグ ON
    adb devices で端末が見えること
"""

import re
import sys
import json
import time
import shutil
import datetime
import subprocess
import pathlib

import cv2
import numpy as np
import uiautomator2 as u2

# exe 化(PyInstaller)された場合は exe のある場所を基準にする。
# そうしないと onefile 版では記録データが一時フォルダに保存され消えてしまう。
if getattr(sys, "frozen", False):
    BASE = pathlib.Path(sys.executable).parent
else:
    BASE = pathlib.Path(__file__).parent
RECIPES = BASE / "recipes"
RECIPES.mkdir(exist_ok=True)


def _resolve_adb():
    """
    使える adb.exe の場所を突き止める。
    1) uiautomator2(adbutils) が使っている adb  ← 接続できているなら確実
    2) PATH 上の adb
    3) このツールと同じフォルダの adb.exe
    """
    try:
        import adbutils
        p = adbutils.adb_path()
        if p and pathlib.Path(str(p)).exists():
            return str(p)
    except Exception:
        pass
    w = shutil.which("adb")
    if w:
        return w
    local = BASE / "adb.exe"
    if local.exists():
        return str(local)
    return "adb"


ADB = _resolve_adb()


# ------------------------------------------------------------------ 接続
def connect(serial=None):
    d = u2.connect(serial) if serial else u2.connect()
    return d


def adb_cmd(serial, *args):
    base = [ADB]
    if serial:
        base += ["-s", serial]
    return base + list(args)


def tap(serial, x, y, hold_ms=0):
    """
    adb の input でタップする。uiautomator2 の d.click より
    ゲームに届きやすい。hold_ms>0 なら「その時間だけ押し続ける」
    タップ（一部のゲームは一瞬のタップを無視するため）。
    """
    x, y = int(x), int(y)
    if hold_ms and hold_ms > 0:
        args = ("shell", "input", "swipe",
                str(x), str(y), str(x), str(y), str(int(hold_ms)))
    else:
        args = ("shell", "input", "tap", str(x), str(y))
    subprocess.run(adb_cmd(serial, *args),
                   capture_output=True, timeout=10)


# ------------------------------------------------ タッチパネルの情報取得
def find_touch_device(serial):
    """
    getevent -lp を解析して
      (デバイスパス, X最大値, Y最大値)
    を返す。見つからなければ (None, None, None)。
    """
    try:
        out = subprocess.run(
            adb_cmd(serial, "shell", "getevent", "-lp"),
            capture_output=True, text=True, timeout=15
        ).stdout
    except Exception as e:
        raise RuntimeError(f"getevent の実行に失敗しました: {e}")

    cur = None
    path = maxx = maxy = None
    for line in out.splitlines():
        m = re.search(r"add device \d+:\s*(\S+)", line)
        if m:
            cur = m.group(1)
            continue
        if ("ABS_MT_POSITION_X" in line) or ("ABS_X " in line):
            mm = re.search(r"max\s+(\d+)", line)
            if mm:
                path, maxx = cur, int(mm.group(1))
        elif ("ABS_MT_POSITION_Y" in line) or ("ABS_Y " in line):
            mm = re.search(r"max\s+(\d+)", line)
            if mm and cur == path:
                maxy = int(mm.group(1))
    return path, maxx, maxy


def map_point(rx, ry, maxx, maxy, sw, sh, swap=False, invx=False, invy=False):
    """生のタッチ座標を画面ピクセル座標へ変換する"""
    if swap:
        rx, ry = ry, rx
        maxx, maxy = maxy, maxx
    x = rx / maxx * sw if maxx else rx
    y = ry / maxy * sh if maxy else ry
    if invx:
        x = sw - x
    if invy:
        y = sh - y
    return int(round(x)), int(round(y))


def auto_swap(maxx, maxy, sw, sh):
    """パネルの向きと画面の向きが食い違うなら True"""
    if not (maxx and maxy):
        return False
    return (maxx > maxy) != (sw > sh)


# --------------------------------------------------- タッチイベント監視
def iter_taps(serial, path, should_stop, on_proc=None, on_ready=None,
              warmup=1.5, debounce=0.25):
    """
    端末のタップ開始を検出するたびに (raw_x, raw_y) を yield する
    ジェネレータ。should_stop() が True になるか proc 終了で止まる。

    getevent はプロセス起動から実際にイベントを流し始めるまで
    わずかに遅れる。warmup 秒だけ待ってから on_ready() を呼び、
    そのあとで監視を始めるので、最初のタップを取りこぼさない。
    """
    proc = subprocess.Popen(
        adb_cmd(serial, "shell", "getevent", "-l", path),
        stdout=subprocess.PIPE, text=True, bufsize=1
    )
    if on_proc:
        on_proc(proc)
    if warmup:
        time.sleep(warmup)
    if on_ready:
        on_ready()

    lastx = lasty = None
    saw_btn = False
    last_emit = 0.0
    try:
        for line in proc.stdout:
            if should_stop():
                break
            parts = line.split()
            if len(parts) < 3:
                continue
            code, val = parts[1], parts[2]

            if code in ("ABS_MT_POSITION_X", "ABS_X"):
                lastx = int(val, 16)
            elif code in ("ABS_MT_POSITION_Y", "ABS_Y"):
                lasty = int(val, 16)
            elif code == "BTN_TOUCH":
                saw_btn = True
                if val == "DOWN" and lastx is not None and lasty is not None:
                    now = time.time()
                    if now - last_emit > debounce:
                        last_emit = now
                        yield lastx, lasty
            elif code == "ABS_MT_TRACKING_ID" and not saw_btn:
                # BTN_TOUCH を出さない端末向けフォールバック
                if val.lower() != "ffffffff" and lastx is not None and lasty is not None:
                    now = time.time()
                    if now - last_emit > debounce:
                        last_emit = now
                        yield lastx, lasty
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


# ------------------------------------------------------- 画像マッチング
def to_gray(pil_img):
    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2GRAY)


def imwrite(path, img):
    """
    cv2.imwrite の代わり。Windows では日本語などを含むパスだと
    cv2.imwrite はエラーも出さず静かに保存に失敗するため、
    imencode + ファイル書き込みで代替する。
    """
    path = pathlib.Path(path)
    ok, buf = cv2.imencode(path.suffix or ".png", img)
    if not ok:
        return False
    path.write_bytes(buf.tobytes())
    return True


def crop(pil_img, cx, cy, w, h):
    """(cx, cy) を中心に w×h で切り抜いた PIL 画像を返す"""
    sw, sh = pil_img.size
    x1 = max(0, cx - w // 2)
    y1 = max(0, cy - h // 2)
    x2 = min(sw, x1 + w)
    y2 = min(sh, y1 + h)
    return pil_img.crop((x1, y1, x2, y2))


def match(screen_gray, tpl_gray, threshold):
    """一致すれば (中心x, 中心y, 一致度)、しなければ (None, None, 一致度)"""
    if (screen_gray.shape[0] < tpl_gray.shape[0] or
            screen_gray.shape[1] < tpl_gray.shape[1]):
        return None, None, 0.0
    res = cv2.matchTemplate(screen_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    if mx < threshold:
        return None, None, mx
    h, w = tpl_gray.shape
    return loc[0] + w // 2, loc[1] + h // 2, mx


# --------------------------------------------------------- レシピ入出力
def recipe_dir(name):
    d = RECIPES / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_recipe(name, data):
    d = recipe_dir(name)
    (d / "recipe.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_recipe(name):
    d = recipe_dir(name)
    data = json.loads((d / "recipe.json").read_text(encoding="utf-8"))
    for step in data["steps"]:
        g = cv2.imread(str(d / step["template"]), cv2.IMREAD_GRAYSCALE)
        if g is None:
            raise FileNotFoundError(f"テンプレートが読めません: {step['template']}")
        step["_gray"] = g
    return data


def list_recipes():
    return sorted(p.name for p in RECIPES.iterdir()
                  if (p / "recipe.json").exists())


# --------------------------------------------------------- 失敗履歴の記録
def append_failure(name, step_label, reason, screenshot):
    """再生失敗を1件、recipes/<name>/failures.jsonl に追記する"""
    d = recipe_dir(name)
    rec = {
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "step_label": step_label,
        "reason": reason,
        "screenshot": screenshot,
    }
    with open(d / "failures.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_failures(name):
    """失敗履歴を新しい順のリストで返す"""
    path = recipe_dir(name) / "failures.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    out.reverse()
    return out
