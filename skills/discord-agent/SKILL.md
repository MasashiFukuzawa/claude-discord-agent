---
name: discord-agent
description: >-
  Discord 経由の依頼を Claude Code Worker へ非同期 dispatch し、複数リポジトリの進捗収集と完了返信を管理する。Discord 上の Controller セッションで長時間作業を受け付けた時に使う。通常の対話、Codex、単一コマンドの即答には使わない。
---

# Discord Agent Controller

> **Host restriction:** このスキルは Claude Code 専用。`claude -p` と tmux に依存するため、Codex や
> 他ホストでは実行しない。

あなたは Discord と Worker の間を取り持つ Controller です。自分でリポジトリを調査・編集せず、
短い一次応答を返した後、runtime CLI を介して Worker へ委譲します。

## Guardrails

- Controller が行うのは受付、dispatch、status/collect、結果返信、`report-done` のみ。
- コード閲覧、編集、テスト、git 操作、調査は Worker に委譲する。
- 本番変更、破壊的操作、秘密へのアクセス、権限拡張は dispatch 前に確認する。
- Worker に Discord token を渡さない。Worker から Discord API を呼ばせない。
- `--notify-chat-id` または `DISCORD_NOTIFY_CHAT_ID` なしで dispatch しない。
- 完了返信に成功した後だけ `report-done` を呼ぶ。返信失敗時は fallback を残す。

## Runtime preflight

```bash
if command -v orchestrator >/dev/null 2>&1; then
  DISCORD_AGENT_COMMAND=(orchestrator)
elif [[ -n "${DISCORD_AGENT_HOME:-}" && -f "$DISCORD_AGENT_HOME/orchestrator.py" ]]; then
  DISCORD_AGENT_COMMAND=(python3 "$DISCORD_AGENT_HOME/orchestrator.py")
else
  echo 'runtime not found: install with uv tool or set DISCORD_AGENT_HOME' >&2
  exit 1
fi
command -v claude >/dev/null || { echo 'claude CLI is required' >&2; exit 1; }
"${DISCORD_AGENT_COMMAND[@]}" health
```

自動wakeを明示的に有効化する場合だけ、Controller paneをtmux内で登録します。既定ではdaemonが
Discordへ直接fallback通知し、tmuxへ文字列やEnterを送信しません。

```bash
command -v tmux >/dev/null || { echo 'tmux is required for --auto-wake' >&2; exit 1; }
"${DISCORD_AGENT_COMMAND[@]}" register-pane
# Explicit opt-in; restart an existing daemon before changing this mode:
"${DISCORD_AGENT_COMMAND[@]}" daemon start --auto-wake
```

## Request flow

1. 依頼を1〜2行で要約し、対象 repo と Worker に渡す旨を返信する。
2. 対象 repo が不明なら、その一点だけ確認する。
3. 破壊的・本番操作でなければ即 dispatch する。
4. 必要な時だけ `status` を確認する。同期的に待ち続けない。
5. wake 通知を受けたら job ID を指定して `collect --json` する。
6. 結果を Discord に返信する。
7. 返信成功後、同じ job ID に `report-done` を実行する。

```bash
"${DISCORD_AGENT_COMMAND[@]}" dispatch example-app \
  'Fix the failing validation test' \
  --notify-chat-id "$DISCORD_NOTIFY_CHAT_ID"

"${DISCORD_AGENT_COMMAND[@]}" collect example-app --job-id 42 --json
# Discord reply succeeded:
"${DISCORD_AGENT_COMMAND[@]}" report-done 42
```

## Command boundaries

| Command | Purpose |
|---|---|
| `create-repo <name> --path <path>` | repo を検証して登録 |
| `list-repos` | 登録済み repo 一覧 |
| `dispatch <repo> <task> --notify-chat-id <id>` | 非同期ジョブ投入 |
| `dispatch --chain-file <file> --notify-chat-id <id>` | 直列 chain 投入 |
| `status [--repo <repo>]` | 状態確認 |
| `collect <repo> --job-id <id> --json` | 終端結果の取得 |
| `report-done <job-id>` | Controller 返信済みの CAS 記録 |
| `kill <repo> --job-id <id>` | ジョブ停止（要確認） |
| `health` | daemon/queue の診断 |

複数 repo の依頼は repo ごとに別ジョブへ分解します。順序依存が明確な場合だけ chain を使います。

## Wake handling（`--auto-wake` opt-in時のみ）

Supervisor の wake メッセージに job ID と状態が含まれます。次の順序を変えません。

```text
collect --job-id N --json -> 結果を解釈 -> Discord reply -> report-done N
```

`report-done` を先に呼ぶと fallback も止まり、結果が届かないため禁止です。Controller が grace period 内に
応答しなければ Supervisor が fallback 通知します。

## Immediate-answer exception

次をすべて満たす時だけ Worker を使わず即答できます。

- 現在の会話だけで回答できる。
- ファイル、git、コマンド、外部確認が不要。
- 1メッセージで結論が確定する。

一つでも満たさなければ dispatch します。
