# 調査記録を自動保存する

`<skill_dir>`、`<topic_dir>`、`<repo_dir>` は絶対パスに置き換える。以下の helper 呼び出しは、ほかの操作とつなげず単独で実行する。

## 初回の回答を保存する

スキルの最初の操作は `status`。未設定なら hooks の追加を含む自動保存の希望と保存先を尋ね、回答を待つ。保存先は既存のローカル Git リポジトリのルート。clone や init は自動では行わない。

```bash
python3 "<skill_dir>/scripts/autosave.py" status
python3 "<skill_dir>/scripts/autosave.py" configure --mode copy --repo "<repo_dir>"
```

「いいえ」と答えた場合は次を実行する。設定は次回も保たれ、再質問しない。

```bash
python3 "<skill_dir>/scripts/autosave.py" configure --mode off
```

保存先の変更も `configure` で明示する。既存の保存物は削除しない。設定は `$XDG_CONFIG_HOME/imozuru/autosave.json`、実行状態は `$XDG_STATE_HOME/imozuru/autosave.json` に置く。未設定の既定値は `~/.config` と `~/.local/state`。テスト用に `IMOZURU_CONFIG_DIR` と `IMOZURU_STATE_DIR` で各 imozuru ディレクトリを変更できる。

## 話題を登録し、保存する

話題のディレクトリができたら登録する。周の終わりと最終回答の前に保存する。

```bash
python3 "<skill_dir>/scripts/autosave.py" register "<topic_dir>"
python3 "<skill_dir>/scripts/autosave.py" sync "<topic_dir>"
```

保存先は `<repo_dir>/research/imozuru/<話題>-<固有ID>/`。同じ名前でも別の絶対パスの話題は別々に保存する。同じ話題を再登録・再保存しても変更の無いファイルは増えない。

対象は `map.md`、`log.md`、`extracts.json`、`digest.json`、`queries.jsonl`、`fetch-log.jsonl`、`watch.tsv`、`watch/*.md`。これらにも抜き書きや検索結果が含まれる。`sources/`、原文を埋め込む生成 HTML、その他のファイル、シンボリックリンクは含めない。保存後に元ファイルが消えても保存先は削除しない。保存先をユーザーが編集した場合は上書きせず失敗を報告する。

成功時の JSON は `copied`、`committed`、`pushed` を別々に示す。失敗時はローカルの調査ファイルを残す。エラーを調べてから明示的に再実行し、保存済みと誤って伝えない。

## 同意された Codex hooks を導入する

初回の「hooks を使って自動保存する」という質問に「はい」と答えた場合は、設定後に hooks を追加する。既定はユーザーの `~/.codex/hooks.json`。環境が別の `CODEX_HOME` を使う場合はそちらの `hooks.json`、プロジェクト限定を希望された場合は `<project>/.codex/hooks.json` の絶対パスを指定する。対象パスとマージ案を表示してから適用する。最初のコマンドはファイルを書かない。

```bash
python3 "<skill_dir>/scripts/autosave.py" install-hooks "<hooks_json>"
python3 "<skill_dir>/scripts/autosave.py" install-hooks "<hooks_json>" --apply
```

適用後にユーザーが Codex の `/hooks` で信頼を承認する必要がある。承認前は「導入済み・信頼待ち」と伝え、hooks が動作中とは報告しない。インストーラーは絶対パスの helper を `PostToolUse` と `Stop` に追加する。同じインストールを繰り返しても重複しない。helper の移動時は古い handler を `/hooks` で取り除き、新しい場所から導入する。

`PostToolUse` は成功を確認できる `Read`、直接の `cat` または `sed -n`、helper の `status` の後に、未設定ならエージェントへ初回確認を促す。任意の shell、失敗、未知の応答形式、単なるパスへの言及は対象にしない。`SkillLoad` イベントはないので、すべてのスキル読み込みを hooks だけで検出することはできない。

成功した単独の `register` コマンドを `PostToolUse` が見たとき、登録済みの話題をイベントの `session_id` に結び付ける。環境に `CODEX_THREAD_ID` がある場合は登録時にも結び付ける。モデルが session ID を推測する必要はない。`Stop` はそのセッションの話題だけを保存する。未登録・不明なセッションや無効時は `{}` を返す。cwd、調査フォルダー全体、会話ログは探索しない。エラーは `systemMessage` で通知し、会話を続行させる block は返さない。

hooks が無い・未信頼・非対応の呼び出し形式の場合も、スキルの初回設定と各周の `sync` が保存を担う。

## 明示された場合だけ commit・push を加える

ユーザーが希望した場合のみ `--mode commit`、または `--mode push --remote "<remote>"` で設定する。push には既存 remote の名前を明示する。設定時の現在ブランチに固定し、develop、detached HEAD、未コミット・ステージ済み・未追跡の変更、merge・rebase 中は拒否する。保存前にも同じ検査を行う。

```bash
python3 "<skill_dir>/scripts/autosave.py" configure --mode commit --repo "<repo_dir>"
python3 "<skill_dir>/scripts/autosave.py" configure --mode push --repo "<repo_dir>" --remote "<remote>"
```

保存したファイルだけを指定して add・commit する。push は設定した remote と branch への通常 push。remote の push URL が設定時から変わった場合も拒否する。branch 切り替え、force push、pull、stash、reset、clean は行わない。失敗後の自動再試行も行わない。

Git の途中で失敗した状態は `status` の `pending_git` に残り、次の `sync` や Stop も未解決エラーを返す。push に失敗した場合、作成したローカル commit は残る。原因と Git の状態を調べ、必要な操作をユーザーの希望に沿って行う。その後、明示的な `configure` で未解決の印を解除する。commit・push モードを再設定するにはリポジトリが clean でなければならない。コピーだけに変更する場合は `configure --mode copy` を使う。再設定自体は失敗した commit や push を再試行しない。
