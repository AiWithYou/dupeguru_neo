永続カタログ
============

永続カタログは、安定したファイルID、パス、内容世代、完全一致の派生成果物、
スキャン履歴、再開可能な作業をSQLiteへ記録します。

基本コマンド
------------

::

   dupeguru catalog scan catalog.sqlite3 Pictures Archive
   dupeguru catalog groups catalog.sqlite3 --page-size 500 > exact-groups.jsonl
   dupeguru catalog changes catalog.sqlite3 --from 12 --to 13 > changes.jsonl
   dupeguru catalog backup catalog.sqlite3 catalog-backup.sqlite3

カタログはローカルファイルシステムへ置いてください。SQLiteのWALを共有上へ
置くことはできません。NAS上のライブラリは、元ファイルシステムが提供する
識別・変更検出機能の範囲でのみ扱えます。

ライブ投影
----------

``catalog groups`` は現在のファイルを再確認する読み取り専用投影です。保存済み
ダイジェストが現在の内容を表さない場合、部分出力を公開せず「再スキャンが必要」
として失敗します。同じカタログとルートで再スキャンしてください。
