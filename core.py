"""
core.py  ―  端末接続・タッチ記録・画像マッチングの共通処理
============================================================
GUI (gui.py) から呼ばれる。単体では起動しない。

依存:
    pip install uiautomator2 opencv-python numpy pillow
前提:
    adb.exe は adbutils に同梱されているものを自動で使うため、別途
    platform-tools を用意する必要はない(_resolve_adb()参照)
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
    1) exe化(PyInstaller onedir)されている場合、TapReplay.specの
       collect_all('adbutils')で同梱したadbutils/binaries/adb.exeを直接指す。
       adbutils.adb_path()は内部でimportlib.resourcesを使ってこのファイルを
       探すが、PyInstallerの疑似importer環境ではこの解決がうまく働かない
       ことがある(パッケージのソースがPYZアーカイブ内にあり、実ファイルの
       サブフォルダと食い違うため)。onedir構成ではsys._MEIPASSが実行時に
       exeと同じ階層に展開された実フォルダ(通常<exeのフォルダ>/_internal)
       を指すため、そこから同梱物の実在するパスを直接組み立てる方が確実
    2) uiautomator2(adbutils) が使っている adb(frozenでない通常実行時。
       開発時の `python gui.py` ではこちらで解決できる)
    3) PATH 上の adb
    4) このツールと同じフォルダの adb.exe

    戻り値: (adbの実行パス, 解決方法を表す短い説明。全滅した場合はNone)
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundled = pathlib.Path(meipass) / "adbutils" / "binaries" / "adb.exe"
            if bundled.exists():
                return str(bundled), "exe同梱(adbutils)"
    try:
        import adbutils
        p = adbutils.adb_path()
        if p and pathlib.Path(str(p)).exists():
            return str(p), "adbutils"
    except Exception:
        pass
    w = shutil.which("adb")
    if w:
        return w, "PATH"
    local = BASE / "adb.exe"
    if local.exists():
        return str(local), "実行フォルダ"
    return "adb", None


ADB, ADB_SOURCE = _resolve_adb()


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


def shot_to_window(x, y, shot_w, shot_h, window_w, window_h):
    """スクリーンショット空間の座標(x, y)を、adb shell input tap が解釈する
    表示解像度(uiautomator2のwindow_size)空間の座標に変換する。

    内部の座標(クリック位置・マッチング結果・dx/dy)はすべてスクリーン
    ショット空間で統一し、端末へタップを実際に送る直前だけこれを通す
    (screenshot()とwindow_size()のサイズが違う端末で、記録・再生位置が
    ずれる不具合の修正)。倍率は縦横で別々に持つ(スクショと表示解像度の
    アスペクト比が違う場合、一律の倍率では正しく変換できないため)。
    """
    if shot_w <= 0 or shot_h <= 0:
        raise ValueError("shot_to_window: shot_w/shot_h は正の値である必要があります")
    scale_x = window_w / shot_w
    scale_y = window_h / shot_h
    return int(round(x * scale_x)), int(round(y * scale_y))


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


MIN_LOCAL_STD = 2.0  # マスク内の局所標準偏差(グレー階調)がこれ未満なら絵柄無しとみなす

# テンプレートのCannyエッジ画素がこの割合未満なら「エッジ手法では判定不能」とみなす。
# 実測で確認した通り、テンプレート側のエッジ画素が0(=分散0)だと
# cv2.matchTemplate(TM_CCOEFF_NORMED)は画面の内容に関わらず退化して
# 常に1.0を返す(OpenCVの0/0特殊扱い)。これがedge:1.00という無意味な
# 「常にマッチ」を生む原因だったため、評価前にテンプレート側で足切りする
MIN_EDGE_RATIO = 0.01

# match()の戻り値がこの範囲を超えて逸脱していたら、算出ロジックの不具合と
# みなして例外にする(-1〜1に収まるはずの一致度が34.01のような値になって
# いても素通りし、無関係な座標をタップし続けていた今回の不具合の再発防止)
_SCORE_RANGE_TOLERANCE = 1e-3


def masked_zncc(img_gray, tpl, mask, min_std=MIN_LOCAL_STD):
    """マスク内の画素だけで平均・分散を正規化した相関を全画面で計算する。

    半透明ボタンやアニメーション背景など、テンプレートの一部の画素しか
    毎フレーム安定していない場面で、その安定した部分(mask)だけを見て
    位置を特定したいときに使う。マスク外の変動(背景アニメ等)に
    引きずられて一致度が下がるのを防げる。

    img_gray: 探索対象の画面(グレースケール)
    tpl:      テンプレート画像(グレースケール、img_gray以下のサイズ)
    mask:     tplと同じサイズ。0より大きい画素だけを判定に使う

    絵柄の無い(局所標準偏差がmin_std未満の)一様な領域は評価対象から外す。
    ZNCCはそうした領域で数学的に未定義であり、分散の引き算(sum_I2 -
    sum_I**2/n)がfloat32の桁落ちでほぼ0や負になったところを小さい値で
    割ってしまうと、わずかな数値誤差が数十倍に増幅されて-1〜1を超える
    あり得ない値(誤検出の原因)になるため。

    戻り値はimg_grayに対するスコアマップ(cv2.matchTemplateの戻り値と
    同じ形状: (H-h+1, W-w+1))。値は必ず-1.0〜1.0にクリップされる。
    """
    I = img_gray.astype(np.float32)
    I -= float(I.mean())  # 桁落ち対策(先に画像全体の平均を引いて値を小さくする)
    T = tpl.astype(np.float32)
    M = (mask > 0).astype(np.float32)
    n = float(M.sum())
    if n < 1:
        raise ValueError("masked_zncc: マスクの有効画素が0です")
    mT = (T * M).sum() / n
    Tz = (T - mT) * M
    denT = float(np.sqrt((Tz * Tz).sum()))
    if denT < 1e-3:
        raise ValueError("masked_zncc: テンプレートのマスク内に絵柄がありません")
    sum_IT = cv2.matchTemplate(I, Tz, cv2.TM_CCORR)
    sum_I  = cv2.matchTemplate(I, M,  cv2.TM_CCORR)
    sum_I2 = cv2.matchTemplate(I * I, M, cv2.TM_CCORR)
    varI = sum_I2 - (sum_I * sum_I) / n
    stdI = np.sqrt(np.maximum(varI, 0.0) / n)
    res = np.zeros_like(sum_IT)
    ok = stdI >= min_std
    res[ok] = sum_IT[ok] / (np.sqrt(varI[ok]) * denT)
    # クリップは微小な浮動小数点誤差を吸収するためのもの。それを超える逸脱は
    # 算出ロジックの不具合なので、丸めて隠さずここで検出する
    # (match()側のレンジチェックはクリップ後の値しか見えず、ここで発散して
    # いてもクリップ後は1.00に丸まって素通りしてしまうため、クリップ前の
    # ここで検証する)
    peak = float(np.abs(res).max())
    if peak > 1.0 + _SCORE_RANGE_TOLERANCE:
        raise ValueError(f"masked_zncc: 一致度が想定範囲外です(最大絶対値={peak})")
    return np.clip(res, -1.0, 1.0)


def peak_match(screen_gray, tpl_gray, method="ccoeff", mask=None):
    """screen_gray全体をmethodで探索し、しきい値に関わらず最も一致した
    位置(中心x, 中心y)とその一致度を返す(見つからない条件でも位置と
    値は返す。テンプレートがscreenより大きい等で計算不能な場合のみ
    (None, None, 0.0))。

    match()の中身そのものだが、しきい値による絞り込みをしないぶん、
    診断用途(失敗時のスクリーンショットへの位置描画など)にも使える。
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
        if np.count_nonzero(tpl_edge) / tpl_edge.size < MIN_EDGE_RATIO:
            # テンプレートにエッジがほぼ無い→退化して常に1.0になるため、
            # 「エッジ手法では判定できない」として未検出扱いにする
            return None, None, 0.0
        res = cv2.matchTemplate(screen_edge, tpl_edge, cv2.TM_CCOEFF_NORMED)
    else:
        raise ValueError(f"match: 未知のmethodです: {method}")

    _, mx, _, loc = cv2.minMaxLoc(res)
    if not (-1.0 - _SCORE_RANGE_TOLERANCE <= mx <= 1.0 + _SCORE_RANGE_TOLERANCE):
        raise ValueError(
            f"match: 一致度が想定範囲(-1〜1)外です(method={method}, 値={mx})。"
            "算出ロジックに問題がある可能性があります")
    mx = float(np.clip(mx, -1.0, 1.0))
    h, w = tpl_gray.shape
    return loc[0] + w // 2, loc[1] + h // 2, mx


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
    cx, cy, mx = peak_match(screen_gray, tpl_gray, method=method, mask=mask)
    if cx is None or mx < threshold:
        return None, None, mx
    return cx, cy, mx


def annotate_diagnostic(img_bgr, tpl_shape, best_loc=None, recorded_pos=None,
                         tapped_pos=None):
    """失敗時のスクリーンショット(BGR)に診断用の情報を描き込んだコピーを返す。

    「正しい場所にマッチしているのにタップが効かない」のか「そもそも
    無関係な場所にマッチしている」のかを、画像だけで切り分けられるようにする。

    tpl_shape:   (h, w) テンプレートのサイズ(矩形の大きさに使う)
    best_loc:    (method, val, cx, cy) その時点で最も一致した位置(赤の矩形)。
                 Noneなら描かない
    recorded_pos: (x, y) レシピに記録されている本来のタップ位置(緑の十字)。
                 Noneなら描かない
    tapped_pos:  (x, y) 実際にタップした座標(黄のバツ印、タップ済みの場合のみ)。
                 Noneなら描かない

    すべて元画像と同じ「スクリーンショット空間」の座標で指定すること。
    """
    img = img_bgr.copy()
    font = cv2.FONT_HERSHEY_SIMPLEX
    if best_loc is not None:
        method, val, cx, cy = best_loc
        th, tw = tpl_shape[:2]
        x0, y0 = int(cx - tw / 2), int(cy - th / 2)
        cv2.rectangle(img, (x0, y0), (x0 + tw, y0 + th), (0, 0, 255), 2)
        cv2.putText(img, f"match:{method} {val:.4f}", (x0, max(15, y0 - 8)),
                    font, 0.55, (0, 0, 255), 2, cv2.LINE_AA)
    if recorded_pos is not None:
        rx, ry = int(recorded_pos[0]), int(recorded_pos[1])
        cv2.drawMarker(img, (rx, ry), (0, 255, 0), cv2.MARKER_CROSS, 26, 2)
        cv2.putText(img, "recorded", (rx + 10, ry - 8),
                    font, 0.55, (0, 255, 0), 2, cv2.LINE_AA)
    if tapped_pos is not None:
        tx, ty = int(tapped_pos[0]), int(tapped_pos[1])
        cv2.drawMarker(img, (tx, ty), (0, 255, 255), cv2.MARKER_TILTED_CROSS, 26, 2)
        cv2.putText(img, "tapped", (tx + 10, ty + 20),
                    font, 0.55, (0, 255, 255), 2, cv2.LINE_AA)
    return img


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
