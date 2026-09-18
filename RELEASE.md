# リリース手順(開発者向け)

GitHub Releases に新しいビルドを公開する手順です。利用者向けのビルド方法・
使い方は `README.md` を参照してください。

## 1. バージョン番号を上げる

`core.py` の `VERSION` 定数を更新する(このリポジトリでバージョンを管理している唯一の場所です)。

```python
VERSION = "1.1.0"
```

この値はアプリのウィンドウタイトル・起動時ログ・配布 ZIP のファイル名
(`TapReplay_v<バージョン>.zip`)にそのまま反映されます。

## 2. ZIP を作る

```bash
build.bat
```

ビルドが成功すると、最後のステップ([7/7])で `dist\TapReplay_v<バージョン>.zip`
が自動生成されます。中身は以下だけです(`recipes\` や `settings.ini` など
開発中に生成されたファイルは含まれません)。

- `TapReplay.exe` と `_internal\`(依存ライブラリ一式)
- `THIRD_PARTY_LICENSES.txt`
- `LICENSE`
- `はじめにお読みください.txt`

開発中の繰り返しビルドで ZIP 化が不要なときは `build.bat nozip` で ZIP 生成
だけをスキップできます(exe 自体は通常どおりビルドされます)。

## 3. ZIP の動作確認

公開前に、必ず**別の場所に展開して**動作確認する。

1. `dist\TapReplay_v<バージョン>.zip` を `dist\` の外(デスクトップなど)にコピー
2. 展開する → 展開直後に `TapReplay\` フォルダが1つできること(ファイルが
   直接散らばらないこと)を確認
3. `TapReplay\TapReplay.exe` をダブルクリックして起動できることを確認
4. `TapReplay\recipes\` が存在しない(開発者のレシピが混入していない)ことを確認
5. ZIP のサイズを確認(GitHub Releases の1ファイルあたりの上限は 2GB。
   現状のビルドは数百MB程度に収まる見込みだが、依存ライブラリが増えたとき
   のために念のため確認する)

## 4. GitHub Releases に公開する

1. GitHub の当該リポジトリで Releases → "Draft a new release"
2. タグを作成: `v<バージョン>`(例: `v1.1.0`。`core.py` の `VERSION` と一致させる)
3. リリースタイトル: `TapReplay v<バージョン>`
4. Assets に `dist\TapReplay_v<バージョン>.zip` をアップロード
5. リリースノートに書く内容:
   - 今回の変更点(機能追加・不具合修正など。箇条書きで簡潔に)
   - 動作要件(Windows / Android 端末で USB デバッグを ON にする必要がある旨)
   - 前バージョンからのアップグレード方法(通常は ZIP を展開し直すだけ。
     `recipes\` や `settings.ini` を残したい場合は、新しい ZIP を別フォルダに
     展開してから古いフォルダの `recipes\` と `settings.ini` をコピーする、
     という手順を明記する)
6. "Publish release"
