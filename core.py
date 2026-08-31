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

import sys
import json
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


# ------------------------------------------------------- 画像マッチング
def to_gray(pil_img):
    # 端末によってはスクリーンショットがRGBA(4チャンネル)で返ることがあり、
    # その場合COLOR_RGB2GRAYは例外になるため、先にRGBへ変換しておく
    return cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2GRAY)


def to_bgr(pil_img):
    """PIL画像をOpenCV(cv2.imwrite/imencode)向けのBGR配列に変換する。
    保存用途(失敗時のスクリーンショット等)はグレースケールより
    カラーの方が原因調査に有用なため、色付きで保存したい場面で使う"""
    return cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2BGR)


def is_distinctive(gray, min_std=35.0):
    """テンプレート画像がほぼ無地(情報量が少ない)かどうかを判定する。
    ほぼ無地の画像は暗転・読み込み画面など無関係な場所にも高い一致度で
    誤検知しやすいため、"このステップの番が来た時の直接マッチ"以外
    (別ステップ・共通ポップアップの探索など)には使わない方が安全"""
    return float(gray.std()) >= min_std


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
    """(cx, cy) を中心に w×h で切り抜いた (PIL画像, (dx, dy)) を返す。

    画面端に近いときは、切り抜きサイズ(w×h)を保ったまま範囲を内側に
    ずらす(端を単純に切り落とすと指定サイズより小さくなり、そのテンプレート
    を使った再生時のタップ位置計算がずれるため)。その結果、切り抜き範囲の
    中心とクリック位置(cx, cy)がずれることがあるため、そのずれを
    (dx, dy) として返す。呼び出し側は再生時、core.match() が返す
    テンプレート中心に (dx, dy) を足すことで、実際にクリックした位置を
    タップできる"""
    sw, sh = pil_img.size
    w = min(w, sw)
    h = min(h, sh)
    x1 = max(0, min(cx - w // 2, sw - w))
    y1 = max(0, min(cy - h // 2, sh - h))
    dx = cx - (x1 + w // 2)
    dy = cy - (y1 + h // 2)
    return pil_img.crop((x1, y1, x1 + w, y1 + h)), (dx, dy)


# 手法ごとのしきい値の目安(参考値)。しきい値の意味は手法ごとに全く異なる
# ため、手法をまたいで使い回さないこと(呼び出し側でmethodに応じて選ぶ)。
# フェーズ3以降、実際のステップごとのしきい値は記録時に実測して自動算出する
# ため(gui.py側)、これはあくまで目安の参考値
DEFAULT_THRESHOLDS = {
    "ccoeff": 0.85,       # 既存レシピ向け。TM_CCOEFF_NORMEDで全画面探索
    "masked_zncc": 0.85,  # 半透明ボタン+アニメ背景など、mask指定時
    "edge": 0.45,         # Cannyエッジベースのフォールバック
}


def masked_zncc(img_gray, tpl, mask):
    """マスク内の画素だけで平均・分散を正規化した相関を全画面で計算する。

    半透明ボタンやアニメーション背景など、テンプレートの一部の画素しか
    毎フレーム安定していない場面で、その安定した部分(mask)だけを見て
    位置を特定したいときに使う。マスク外の変動(背景アニメ等)に
    引きずられて一致度が下がるのを防げる。

    img_gray: 探索対象の画面(グレースケール)
    tpl:      テンプレート画像(グレースケール、img_gray以下のサイズ)
    mask:     tplと同じサイズ。0より大きい画素だけを判定に使う

    戻り値はimg_grayに対するスコアマップ(cv2.matchTemplateの戻り値と
    同じ形状: (H-h+1, W-w+1))。
    """
    I = img_gray.astype(np.float32)
    T = tpl.astype(np.float32)
    M = (mask > 0).astype(np.float32)
    n = M.sum()
    if n <= 0:
        raise ValueError("masked_zncc: mask が全て0です(有効画素がありません)")
    mT = (T * M).sum() / n
    Tz = (T - mT) * M
    denT = np.sqrt((Tz * Tz).sum())
    if denT <= 0:
        raise ValueError(
            "masked_zncc: mask内のテンプレートがほぼ無地です(分散が0)")
    sum_IT = cv2.matchTemplate(I, Tz, cv2.TM_CCORR)
    sum_I  = cv2.matchTemplate(I, M,  cv2.TM_CCORR)
    sum_I2 = cv2.matchTemplate(I * I, M, cv2.TM_CCORR)
    varI = np.maximum(sum_I2 - (sum_I * sum_I) / n, 1e-6)
    return sum_IT / (np.sqrt(varI) * denT)


def match(screen_gray, tpl_gray, threshold, method="ccoeff", mask=None):
    """一致すれば (中心x, 中心y, 一致度)、しなければ (None, None, 一致度)。

    method:
        "ccoeff"      - 既存方式。TM_CCOEFF_NORMEDで全画面探索(デフォルト、
                        既存レシピはこのまま動く)
        "masked_zncc" - 半透明ボタンなど、テンプレートの一部だけで判定
                        したい場合に使う。mask(tpl_grayと同サイズ、
                        0より大きい画素を有効とする)が必須
        "edge"        - 画面・テンプレート双方にCanny(60,160)をかけてから
                        TM_CCOEFF_NORMED。背景の色そのものの変化に
                        左右されにくいフォールバック

    しきい値の意味は手法ごとに異なるため(目安はDEFAULT_THRESHOLDSを参照)、
    手法をまたいでしきい値を使い回さないこと。
    """
    if (screen_gray.shape[0] < tpl_gray.shape[0] or
            screen_gray.shape[1] < tpl_gray.shape[1]):
        return None, None, 0.0

    if method == "ccoeff":
        res = cv2.matchTemplate(screen_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
    elif method == "masked_zncc":
        if mask is None:
            raise ValueError("match: method='masked_zncc' には mask が必須です")
        res = masked_zncc(screen_gray, tpl_gray, mask)
    elif method == "edge":
        screen_edge = cv2.Canny(screen_gray, 60, 160)
        tpl_edge = cv2.Canny(tpl_gray, 60, 160)
        res = cv2.matchTemplate(screen_edge, tpl_edge, cv2.TM_CCOEFF_NORMED)
    else:
        raise ValueError(f"match: 未知のmethodです: {method}")

    _, mx, _, loc = cv2.minMaxLoc(res)
    if mx < threshold:
        return None, None, mx
    h, w = tpl_gray.shape
    return loc[0] + w // 2, loc[1] + h // 2, mx


# --------------------------------------------------------- レシピ入出力
def recipe_path(name):
    """recipes/<name> のパスを返すだけで、フォルダは作成しない。
    存在確認や読み取りだけの処理で使うこと(recipe_dir()と違い、
    存在しないレシピ名を指定しても空フォルダが作られない)"""
    return RECIPES / name


def recipe_dir(name):
    """recipes/<name> のパスを返す。無ければ作成する(書き込み用)"""
    d = recipe_path(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_recipe(name, data):
    d = recipe_dir(name)
    (d / "recipe.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_step_images(d, step, label):
    """テンプレート(と、あれば手法用のマスク)を読み込んで
    step["_gray"]/step["_mask"] にセットする。

    "method"キーを持たない古いレシピはccoeffとして扱い、マスクは
    読み込まない(既存レシピの再生を壊さないため)"""
    g = cv2.imread(str(d / step["template"]), cv2.IMREAD_GRAYSCALE)
    if g is None:
        raise FileNotFoundError(f"{label}のテンプレートが読めません: {step['template']}")
    step["_gray"] = g
    step.setdefault("method", "ccoeff")
    mask_name = step.get("mask")
    if mask_name:
        m = cv2.imread(str(d / mask_name), cv2.IMREAD_GRAYSCALE)
        if m is None:
            raise FileNotFoundError(f"{label}のマスクが読めません: {mask_name}")
        step["_mask"] = m


def load_recipe(name):
    d = recipe_path(name)
    data = json.loads((d / "recipe.json").read_text(encoding="utf-8"))
    for step in data["steps"]:
        _load_step_images(d, step, "ステップ")
    data.setdefault("popups", [])
    for popup in data["popups"]:
        _load_step_images(d, popup, "ポップアップ")
    return data


def list_recipes():
    return sorted(p.name for p in RECIPES.iterdir()
                  if (p / "recipe.json").exists())


# --------------------------------------------------------- 失敗履歴の記録
def append_failure(name, step_label, reason, screenshot, attempts=None):
    """再生失敗を1件、recipes/<name>/failures.jsonl に追記する。

    attempts: そのステップの検出で試した手法ごとの最高一致度の記録
    (例: [{"method": "masked_zncc", "score": 0.62, "threshold": 0.75}, ...])。
    「どの手法が実際に効いているか」を失敗履歴タブで後から判断できるように
    残しておく任意項目(旧レシピ・呼び出し元がまだ対応していない場合はNone)"""
    d = recipe_dir(name)
    rec = {
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "step_label": step_label,
        "reason": reason,
        "screenshot": screenshot,
    }
    if attempts:
        rec["attempts"] = attempts
    with open(d / "failures.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_failures(name):
    """失敗履歴を新しい順のリストで返す"""
    path = recipe_path(name) / "failures.jsonl"
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
