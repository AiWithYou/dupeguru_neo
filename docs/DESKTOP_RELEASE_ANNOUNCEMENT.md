# デスクトップ版リリース告知テンプレート

状態: **下書き（未公開）**

この文書は、簡略化したデスクトップUIを公開するときのGitHubプレリリース文と
X投稿文です。`{...}` のプレースホルダーをすべて置き換え、対象コミットのCIが
成功してから使用してください。正式な署名済み安定版としては公開しません。

## 公開前に確定する情報

| 項目 | 値 |
| --- | --- |
| バージョン | `{VERSION}` |
| プレリリースタグ | `{DESKTOP_TAG}` |
| 対象コミット（40桁） | `{COMMIT_SHA}` |
| 公開日 | `{RELEASE_DATE}` |
| GitHubリリースURL | `{RELEASE_URL}` |
| Windows EXE | `{WINDOWS_ASSET}` |
| Windows SHA-256 | `{WINDOWS_SHA256}` |
| macOS APP ZIP | `{MACOS_ASSET}` |
| macOS SHA-256 | `{MACOS_SHA256}` |

対応範囲:

- Windows 10 / 11、64ビット（x86_64）
- macOS 15で生成・起動確認したApple Silicon版（arm64）
- Intel Mac（x86_64）は対象外
- WindowsはAuthenticode未署名
- macOS APPはad-hoc署名のみで、Appleの公証は未取得

## GitHubプレリリース

### タイトル

```text
dupeGuru Neo {VERSION} Desktop Preview — 迷わない重複確認UI
```

### 本文

```markdown
dupeGuru Neoのデスクトップ操作を、初めてでも流れが分かる2ステップへ整理しました。

1. 調べるフォルダーを選ぶ
2. 重複を検索する

結果画面では、各グループの先頭に「残すファイル」を置き、余分なコピーだけを
チェックできるようにしました。選択件数と容量を常に表示し、現在の完全な
スキャンでバイト一致を確認したファイルだけを、復元可能な隔離へ進められます。

### 主な変更

- フォルダー選択から検索までを、番号付きの2ステップに整理
- フォルダーの扱いを「整理する／すべて残す／比較だけ／対象外」で明示
- 結果画面にチェック列、全選択・全解除、選択件数と容量を追加
- 残すファイルと隔離候補を文章でも説明
- 保存済み結果、類似結果、不完全なスキャンからの隔離を無効化
- 詳細表示ボタンとフォルダーのドラッグ＆ドロップ後の表示更新を修正

### ダウンロード

- Windows 10 / 11 x86_64: `{WINDOWS_ASSET}`
  - SHA-256: `{WINDOWS_SHA256}`
- macOS Apple Silicon arm64: `{MACOS_ASSET}`
  - SHA-256: `{MACOS_SHA256}`

Windowsはダウンロードした`dupeguru-neo-...-unsigned.exe`をダブルクリックして
実行してください。ZIPの展開やインストールは不要です。

macOSはAPP ZIPを展開し、`dupeguru-neo.app`をApplicationsへ移動してください。
初回はControlキーを押しながらアプリをクリックして「開く」を選びます。
Intel Mac向けではありません。

### 署名と確認範囲

これは未署名のデスクトップ・プレリリースです。WindowsはAuthenticode未署名、
macOS APPはad-hoc署名のみでAppleの公証を受けていないため、SmartScreenまたは
Gatekeeperが警告を表示する場合があります。

Windows版はWindows 11でUI表示、テスト、配布EXEの生成・起動確認を行っています。
macOS版はmacOS 15のGitHub ActionsでAPP生成、パッケージ検証、オフスクリーン
起動確認まで成功していますが、Mac実機での手動デバッグは行っていません。

Built from `{COMMIT_SHA}`.

---

English summary:

This desktop preview introduces a simpler two-step workflow: choose folders,
then scan. Each result group clearly identifies the keeper, and only extra
copies can be checked. Recoverable quarantine remains limited to byte-exact
files from the current complete scan.

Platforms: 64-bit Windows 10/11 and Apple Silicon macOS. These builds are not
officially signed or notarized. Intel Macs are not supported.
```

## X投稿

本文（`{RELEASE_URL}`を実際のURLへ置換）:

```text
dupeGuru NeoのデスクトップUIを、迷いにくい2ステップに刷新しました。
①フォルダーを選ぶ ②重複を検索

残すファイルと余分なコピーを明示し、完全一致だけを復元可能な隔離へ。
Windows 10/11・Apple Silicon Mac向けの未署名プレビューです。

{RELEASE_URL}
#dupeGuruNeo
```

画像の代替テキスト:

```text
dupeGuru NeoのWindows画面。フォルダーを選んで重複を検索する2段階の構成と、
残すファイルを先頭に表示して余分なコピーだけをチェックする結果画面が並んでいる。
```

推奨添付画像:

- `docs/images/ja/main-window.png`
- `docs/images/ja/results-window.png`

## 公開チェックリスト

- [ ] `{VERSION}` と `{DESKTOP_TAG}` を確定し、対象コミットと一致させた
- [ ] `{COMMIT_SHA}` が40桁で、`master` 上の公開対象コミットと一致している
- [ ] 対象コミットのGitHub Actions全体が成功している
- [ ] Windows EXEとmacOS APPの生成・起動確認ジョブが成功している
- [ ] 公開するEXEとAPP ZIPが、成功したCIで検証されたものとバイト単位で同一である
- [ ] 各配布物のSHA-256 sidecarを再確認した
- [ ] Windows 10 / 11 x86_64とmacOS arm64以外を対応対象として記載していない
- [ ] 未署名、未公証、Intel Mac非対応の注意書きを残した
- [ ] `rg -n "\{[A-Z0-9_]+\}" docs/DESKTOP_RELEASE_ANNOUNCEMENT.md` の結果が空である
- [ ] GitHubリリースをプレリリースとして公開し、URLと各アセットを確認した
- [ ] GitHubリリース公開後にだけ、確定URLを入れたX投稿を行う

公開手順と成果物の信頼境界は
[`docs/RELEASE.md`](RELEASE.md)を優先してください。
