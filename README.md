# ScurityNewsNotification

`http://izumino.jp/Security/sec_trend.cgi` と `https://www.security-next.com/feed` を取得し、前日分のニュースを抽出して通知するための最小構成です。

## ローカル実行

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python fetch_security_news.py --previous-day-all --output text
```

## メール通知（SMTP）

以下の環境変数を設定して `--notify-email` を指定します。

- `SMTP_HOST`（必須）
- `SMTP_PORT`（任意、既定 `587`）
- `SMTP_USERNAME`（任意）
- `SMTP_PASSWORD`（任意）
- `EMAIL_FROM`（必須）
- `EMAIL_TO`（必須、複数はカンマ区切り）
- `SMTP_SSL`（任意: `true/false`、既定 `false`）
- `SMTP_STARTTLS`（任意: `true/false`、既定 `true`）

```bash
export SMTP_HOST="smtp.example.com"
export SMTP_PORT="587"
export SMTP_USERNAME="user"
export SMTP_PASSWORD="pass"
export EMAIL_FROM="from@example.com"
export EMAIL_TO="to1@example.com,to2@example.com"
python fetch_security_news.py --previous-day-all --notify-email --suppress-duplicate --notify-only-new --state-file .state/last_notification.json --fail-soft
```

重複通知を抑止する場合、`--suppress-duplicate` を指定すると前回通知内容のハッシュを `--state-file` に保存し、同一内容なら送信をスキップします。

新規ニュースのみ通知したい場合は `--notify-only-new` を指定します。`--state-file` に保存した `sent_item_keys` を使って、過去に送信済みの項目を除外します。

## Groq API関連度評価

ニュースをGroq APIで評価し、関連度が高いものだけ通知・出力する場合は以下を設定します。

- `GROQ_API_KEY`（必須）
- `GROQ_API_BASE`（任意、既定 `https://api.groq.com/openai/v1`）
- `GROQ_MODEL`（任意、既定 `llama-3.3-70b-versatile`）
- `GROQ_EVAL_MIN_INTERVAL_SEC`（任意、既定 `3.0`）
- `GROQ_EVAL_MAX_RETRIES`（任意、既定 `6`）
- `GROQ_EVAL_MAX_BACKOFF_SEC`（任意、既定 `90`）

```bash
export GROQ_API_KEY="gsk_..."
export GROQ_API_BASE="https://api.groq.com/openai/v1"
export GROQ_MODEL="llama-3.3-70b-versatile"
export GROQ_EVAL_MIN_INTERVAL_SEC="3.0"
export GROQ_EVAL_MAX_RETRIES="8"
export GROQ_EVAL_MAX_BACKOFF_SEC="120"
python fetch_security_news.py --previous-day-all --notify-email --suppress-duplicate --notify-only-new --evaluate-with-groq --relevance-threshold 0.65 --fail-soft
```

`--evaluate-with-groq` を指定すると、各ニュースの `relevance_score` を算出し、`--relevance-threshold` 以上の項目のみ残します。キー未設定時は評価をスキップします。

評価結果のJSONスキーマは固定です。
- `score`: 0〜1.0
- `name`: scoreが0.9以上のときの組織名
- `summary`: scoreが0.9以上のときの記事要約（100字以内）

Groq評価はニュース1件ずつ順次実行し、前日分の評価がすべて完了した後に1通のメールへまとめて送信します。
429/5xx が返る場合は、間隔制御と指数バックオフで自動再試行します。

## Google Gemini関連度評価（任意）

Groqの代わりにGoogle Geminiを使う場合は以下を設定します。

- `GOOGLE_API_KEY`（必須）
- `GOOGLE_API_BASE`（任意、既定 `https://generativelanguage.googleapis.com/v1beta`）
- `GOOGLE_MODEL`（任意、既定 `gemini-2.5-flash`）
- `GOOGLE_EVAL_MIN_INTERVAL_SEC`（任意、既定 `3.0`）
- `GOOGLE_EVAL_MAX_RETRIES`（任意、既定 `3`）
- `GOOGLE_EVAL_MAX_BACKOFF_SEC`（任意、既定 `90`）

```bash
export GOOGLE_API_KEY="AIza..."
export GOOGLE_MODEL="gemini-2.5-flash"
python fetch_security_news.py --previous-day-all --notify-email --suppress-duplicate --notify-only-new --evaluate-with-google-studio --relevance-threshold 0.9 --fail-soft
```

`--evaluate-with-groq` と `--evaluate-with-google-studio` を同時指定した場合は、Groqが優先されます。

`--previous-day-all` は、取得結果の中で最も新しい日付の1日前の日付に一致するニュースを全件抽出します。

## GitHub Actions

- Workflow: `.github/workflows/security-news-check.yml`
- 推奨シークレット（通知有効化時）:
  - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`
  - `EMAIL_FROM`, `EMAIL_TO`, `SMTP_SSL`, `SMTP_STARTTLS`
- Groq評価を有効化する場合:
  - `GROQ_API_KEY`
  - `GROQ_API_BASE`（任意）
  - `GROQ_MODEL`（任意）
- Google評価を有効化する場合:
  - `GOOGLE_API_KEY`
  - `GOOGLE_API_BASE`（任意）
  - `GOOGLE_MODEL`（任意）
- シークレット未設定時は通知をスキップしてジョブは継続します。
- `.state/last_notification.json` を Actions cache に保存・復元し、定期実行でも重複抑止が継続します。
