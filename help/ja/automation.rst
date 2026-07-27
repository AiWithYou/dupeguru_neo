自動化CLI
=========

``dupeguru`` はQtに依存しないJSON／JSONL CLIです。元ライブラリへ作用する
計画は、 ``--execute`` を指定しない限り検証だけを行います。

完全一致の例
------------

::

   dupeguru scan Pictures Archive --format jsonl > scan.jsonl
   dupeguru plan scan.jsonl --operation quarantine > plan.jsonl
   dupeguru apply plan.jsonl --dry-run
   dupeguru apply plan.jsonl --execute

復元と確定
----------

::

   dupeguru quarantine list Pictures Archive
   dupeguru quarantine restore path/to/operation-plan.json --dry-run
   dupeguru quarantine restore path/to/operation-plan.json --execute
   dupeguru quarantine finalize path/to/operation-plan.json --dry-run

``finalize`` は復元不能な完全削除です。対象となる1操作を明示し、隔離内容と
バックアップを確認してから実行してください。

終了コードと出力
----------------

上限到達や部分結果は構造化された状態と終了コードで報告されます。自動化では
標準出力だけで成功扱いせず、終了コード、完全性、スキーマバージョンを検証して
ください。
