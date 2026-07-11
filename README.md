# claude-discord-agent

Claude Code を Discord から操作する、複数リポジトリ対応のローカルジョブコントローラーです。
Controller（Claude Code セッション）、Worker（`claude -p`）、Supervisor（Python daemon）を分離し、
長時間タスクを非同期実行して完了結果を Discord に返します。

> [!IMPORTANT]
> このプロジェクトは **Claude Code 専用**です。Worker 起動と Controller の wake-up は Claude Code CLI と
> tmux の挙動に依存します。Codex や他のエージェントホストとの互換性は提供しません。

## Architecture

```text
Discord <-> Controller (Claude Code in tmux)
                  | dispatch / collect / report-done
                  v
             orchestrator.py
                  | SQLite queue
                  v
            Supervisor daemon
                  | claude -p
                  v
                Worker
```

- Controller は受付、ジョブ投入、結果の返信だけを担当します。
- Worker は登録済みリポジトリ内で独立して作業し、構造化結果を stdout に返します。
- Supervisor はジョブを監視し、Controller が応答しない場合に Discord へ fallback 通知します。
- Bot token やリポジトリ一覧は Git 管理しません。

## Requirements

- Python 3.12+
- Claude Code CLI（`claude`）
- tmux
- Discord bot token と送信先 channel ID
- 開発時のみ: [uv](https://docs.astral.sh/uv/)

## Install

```bash
git clone https://github.com/MasashiFukuzawa/claude-discord-agent.git "$HOME/.local/share/claude-discord-agent"
cd "$HOME/.local/share/claude-discord-agent"
uv sync --all-extras
```

Claude Code plugin として使う場合は、このリポジトリを marketplace に追加して
`discord-agent` plugin をインストールしてください。runtime の場所は環境変数で明示します。

```bash
export DISCORD_AGENT_HOME="$HOME/.local/share/claude-discord-agent"
export DISCORD_BOT_TOKEN="..."                 # shell/secret manager から注入
export DISCORD_NOTIFY_CHAT_ID="<channel-id>"
```

token をファイルで管理する場合は
`${XDG_CONFIG_HOME:-$HOME/.config}/claude-discord-agent/env` を `0600` で作成します。
別の場所は `DISCORD_AGENT_ENV_FILE` で指定できます。runtime は現在ユーザー所有の通常ファイルかつ
group/other 権限がないことを検証し、symlink・他ユーザー所有・`0644` 等のファイルを拒否します。

```dotenv
DISCORD_BOT_TOKEN=replace-with-your-token
```

## Quick start

```bash
cd "$DISCORD_AGENT_HOME"
python3 orchestrator.py daemon start             # safe default: direct Discord fallback
python3 orchestrator.py register-pane          # tmux 内で実行
python3 orchestrator.py create-repo example-app --path "$HOME/src/example-app"
python3 orchestrator.py dispatch example-app 'Run the tests and fix the failure' \
  --notify-chat-id "$DISCORD_NOTIFY_CHAT_ID"
python3 orchestrator.py status
python3 orchestrator.py collect example-app --json
```

状態 DB と controller pane は `${XDG_STATE_HOME:-$HOME/.local/state}/claude-discord-agent/` に保存されます。
登録リポジトリは DB が正本です。`config/repos.json` は export/import 用で、`.gitignore` 対象です。

## Safety model

- 登録対象は `git rev-parse --show-toplevel` で検証します。
- token は環境変数または repo 外の設定ファイルからのみ読み込みます。
- Worker は Discord API を呼びません。通知は Supervisor/Controller に集約します。
- 本番操作、破壊的操作、権限拡張は Worker に渡す前にユーザー確認が必要です。
- Worker は Claude Code の通常の権限境界内で起動します。runtime から
  `--dangerously-skip-permissions` を付与する設定は提供しません。
- Supervisor の tmux `send-keys` wake は既定で無効です。通常はdaemonがDiscordへ直接完了通知します。
  `daemon start --auto-wake` は、positive allowlistで通常入力待ちを確認できた時だけwakeしますが、
  UI判定に依存するため必要性を理解した運用者だけが明示的に有効化してください。未知画面では送信せず
  fallback通知へ移行します。

## Development

```bash
uv run ruff check .
uv run ty check lib orchestrator.py
uv run pytest
./scripts/check-public-content.sh
```

詳細な Controller 手順は [skills/discord-agent/SKILL.md](skills/discord-agent/SKILL.md) を参照してください。

## License

Apache License 2.0. See [LICENSE](LICENSE).
