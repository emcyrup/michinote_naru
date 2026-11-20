# 事業所「成」（職員向け）の環境：開発と本番

2026-11-20 にプロバイダが用意した環境へ、このリポジトリ（`emcyrup/michinote_naru`）の `main` を配備します。

| | 開発（dev） | 本番（prod） |
|---|---|---|
| アドレス（職員向け・このリポジトリ） | https://st-naru.ai-labo.cloud/ | https://naru.ai-labo.cloud/ |
| Gunicorn | `127.0.0.1:8049` | `127.0.0.1:8049` |
| DB／ユーザー | `naru_app`／`naru_runtime` | `naru_app_prod`／`naru_runtime_prod` |
| 置き場所 | `~/michinotenaru` | `~/michinotenaru` |
| Python の環境 | `~/envnaru`（成専用） | `~/envnaru`（成専用） |
| 配備 | Actions「Deploy (naru)」→ `target=dev` | Actions「Deploy (naru)」→ `target=prod`（ブランチ `main` だけ） |
| 管理 | Actions「Manage (naru)」→ `target=dev` | Actions「Manage (naru)」→ `target=prod` |


保護者向け（このリポジトリの別のブランチで開発する。Deploy (naru) の `app=family`）：

| | 開発（dev） | 本番（prod） |
|---|---|---|
| アドレス | https://st-naru-family.ai-labo.cloud/ | https://naru-family.ai-labo.cloud/ |
| Gunicorn | `127.0.0.1:8050` | `127.0.0.1:8050` |
| 置き場所／Python の環境 | `~/michinotenarufamily`／`~/envnarufamily` | 同じ |
| DB・写真や書類の置き場所 | 職員向けと共用（`naru_app`・`~/michinotenaru/media`） | 職員向けと共用（`naru_app_prod`・`~/michinotenaru/media`） |
| 配備できるブランチ | どれでも | `main` か `family` |

- **DB の形（migration）は職員向け（`main`）の配備が決めます。** 保護者向けの配備は migrate をしません。保護者向けのブランチで migration を足したら、先に `main` に取り込んで `app=staff` を配備してから、`app=family` を配備します（DB に無い migration があると、配備は止まります）。
- 保護者向けは、職員向けを配備したあとでないと配備できません。
- 保護者向けのブランチができるまでは、`main` のコード（職員向けと同じ画面）を置いて、アドレスがつながることだけ確かめておきます。
- Manage (naru) の `app=family` でできるのは `status`・`autostart_check`・`autostart_install`・`error_log`・`login_log` だけです（データの作業は `app=staff` で）。
- Python の環境は、同じサーバーのほかの事業所と共用の `~/env` を使わず、成専用の `~/envnaru` を使います（無ければ初回の配備で `~/env` の Python から作る）。成の `requirements.txt` を変えても、ほかの事業所に効きません。
- アドレス・ポート・DB 名・DB ユーザーはワークフローに書いてあり、入力欄はありません（開発と本番の取り違えを防ぐため）。サーバーの `.env` のアドレス・DB 名が配備先と違えば、配備は止まります。
- 秘密の値（DB パスワード・SSH 鍵・管理者のパスワード・API キー）は GitHub Secrets とサーバーの `.env` だけに置きます。**このリポジトリは公開です。**

## 1. GitHub Secrets（Settings → Secrets and variables → Actions）

| 名前 | 入れるもの | 要る場面 |
|---|---|---|
| `NARU_DEV_DB_PASSWORD` | 開発の DB ユーザー `naru_runtime` のパスワード | 開発の初回の配備 |
| `NARU_PROD_DB_PASSWORD` | 本番の DB ユーザー `naru_runtime_prod` のパスワード | 本番の初回の配備 |
| `NARU_PROD_DEPLOY_HOST`・`NARU_PROD_DEPLOY_USER`・`NARU_PROD_DEPLOY_SSH_KEY` | 本番のサーバーの SSH（ホスト名・ユーザー・秘密鍵） | 本番の配備・管理（**ほかの Secrets では代えません**） |
| `NARU_DEV_DEPLOY_HOST`・`NARU_DEV_DEPLOY_USER`・`NARU_DEV_DEPLOY_SSH_KEY` | 開発のサーバーの SSH | 開発の配備・管理。無ければ `HOUDEIDEMO_DEPLOY_*`、その次に `DEV_DEPLOY_*` を使う（試用環境と同じサーバーのとき） |
| `NARU_DEV_ADMIN_PASSWORD`・`NARU_PROD_ADMIN_PASSWORD` | 管理者（職員）のパスワード。無ければ `NARU_ADMIN_PASSWORD` | Manage (naru) の `create_facility`・`set_password` |
| `NARU_ANTHROPIC_API_KEY`（環境ごとに分けるなら `NARU_DEV_…`・`NARU_PROD_…`） | Anthropic の API キー | 任意。無いと AI の機能（日誌の下書きなど）が使えない |
| `NARU_DEV_DEPLOY_SSH_PORT`・`NARU_PROD_DEPLOY_SSH_PORT`・`NARU_APP_DIR`・`NARU_VENV`・`NARU_BASE_VENV` | SSH のポート（既定 22）・置き場所・Python の環境 | 既定と違うときだけ |

## 2. 配備

1. Actions → **Deploy (naru)** → Run workflow → `target` を選ぶ（本番はブランチ `main`）。
   テスト → SSH → 初回は `~/envnaru` と `.env` を作る（`SECRET_KEY` は生成、DB パスワードは Secrets から）→ `pip` → `migrate` → `collectstatic` → Gunicorn の起動 → `https://<アドレス>/healthz/` の確認。
2. Manage (naru) → `task=autostart_install`（サーバーの再起動のあとに自動で上がる・5 分ごとの見回り・毎日 3:15 のバックアップ）。
3. Manage (naru) → `task=create_facility`（事業所名「成」・管理者 `naru` が既定。パスワードは Secrets）。作られた事業所の ID は `task=list` で確かめる。
4. Manage (naru) → `task=family_page_on`・`facility_id=<ID>`（職員のホームをアイコンの並びにし、保護者ページを使う。成の画面にするのに必要）。

保護者向けは、そのあと Deploy (naru) → `app=family`、Manage (naru) → `app=family`・`task=autostart_install`。

2 回目からは 1 だけ。`.env` は上書きしません。

- DB パスワードを変えたとき：Secrets を直し、Deploy (naru) の `update_db_password` に印を付けて実行。
- 切り戻し：Deploy (naru) の `ref` に前のコミットの番号（`Manage (naru)` → `status` に出る）を入れて実行。DB の変更を伴うときは `docs/OPERATIONS.md`。
- 本番には見本のデータを入れません（Manage (naru) は本番で `seed_demo`・`billing_demo`・`delete_facility` を止めます）。

## 3. うまくいかないとき

| 出かた | 見るところ |
|---|---|
| `migrate` で権限のエラー（permission denied for schema public など） | DB ユーザーに、表を作る権限（その DB の `public` スキーマの CREATE）が要る。プロバイダに依頼 |
| サーバーの中の healthz は ok、最後の Health check だけ失敗 | nginx の中継先（`127.0.0.1:8049`・`X-Forwarded-Proto`）をプロバイダに確かめる |
| ログインのあと行ったり来たりする | nginx が `X-Forwarded-Proto: https` を付けていない。付けてもらうか、`.env` の `SECURE_SSL_REDIRECT=False` |
| `venv を作れません` | サーバーに Python 3.12 以上が要る。プロバイダに `~/envnaru` を作ってもらう |
| 画面のエラー | Manage (naru) → `error_log` |
