"""
notify.py ― 放置実行の状況を知らせる Windows トースト通知
============================================================
再生スレッド(gui.PlayerThread)から呼ばれる。呼び出し元は再生の本体
ループなので、ここでの処理が再生を遅らせたり止めたりしてはならない。
そのため送信は必ず専用のバックグラウンドスレッドで行い、通知ライブラリの
例外・OS側の通知無効設定など、あらゆる失敗はこの中で握りつぶす
(呼び出し元には一切伝播しない)。

ライブラリ選定(win11toast / windows-toasts をどちらもexe化して実機確認済み):
    両方ともPyInstallerでexe化した状態で通知が実際に表示されることを
    確認した。windows-toastsを採用したのは、show_toast()がその場で
    返る(通知が消える/クリックされるのを待たない)非ブロッキングAPIで
    あるのに対し、win11toastの主要API toast()はデフォルトで内部
    asyncio.run()により通知が消える/クリックされるまでブロックする
    仕様のため(非ブロッキングにするには低レベルのnotify()を使う必要が
    あり、将来の変更で誤って toast() に戻すと再生が止まってしまう事故が
    起きやすい)。再生スレッドを絶対にブロックしないという要件に対して、
    非ブロッキングが標準APIであるwindows-toastsの方が事故りにくいと判断した。

    win10toastは2018年から更新が止まっているため候補から除外した。

同梱(PyInstaller)時の注意:
    windows-toastsの依存であるwinrt(pywinrt)は、名前空間パッケージ直下に
    .pyd拡張モジュールを置く構成で、TapReplay.specでcollect_all
    ('windows_toasts')・collect_all('winrt')を明示していないと
    「開発環境では動くがexeでは無反応」になることを実機ビルドで確認済み。
"""
import threading

APP_NAME = "TapReplay"

_toaster = None
_toaster_failed = False
_init_lock = threading.Lock()


def _get_toaster():
    """WindowsToasterを遅延生成して使い回す。生成に一度失敗したら、以後は
    毎回同じ失敗を繰り返さないよう再試行しない(通知が無効な環境で
    呼び出しのたびに時間を浪費しないため)"""
    global _toaster, _toaster_failed
    if _toaster is not None or _toaster_failed:
        return _toaster
    with _init_lock:
        if _toaster is not None or _toaster_failed:
            return _toaster
        try:
            from windows_toasts import WindowsToaster
            _toaster = WindowsToaster(APP_NAME)
        except Exception:
            _toaster_failed = True
    return _toaster


def _send(title, body):
    from windows_toasts import Toast
    toaster = _get_toaster()
    if toaster is None:
        raise RuntimeError("通知ライブラリの初期化に失敗しています")
    t = Toast()
    t.text_fields = [title, body]
    toaster.show_toast(t)


def notify_async(title, body, on_error=None):
    """トースト通知を1件送る(非ブロッキング)。

    実際の送信は専用のバックグラウンドスレッドで行うため、この関数自体は
    ほぼ即座に返る(=再生スレッドを止めない)。送信中に例外が起きても
    再生には一切影響させず、on_errorが指定されていればそこへ1行だけ
    エラー内容を渡す(GUI側のログへ出す用途を想定。on_error自体が例外を
    投げても再生を止めないよう、ここで握りつぶす)。
    """
    def _run():
        try:
            _send(title, body)
        except Exception as e:
            if on_error is not None:
                try:
                    on_error(f"通知の送信に失敗しました: {e}")
                except Exception:
                    pass

    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception as e:
        if on_error is not None:
            try:
                on_error(f"通知の開始に失敗しました: {e}")
            except Exception:
                pass
