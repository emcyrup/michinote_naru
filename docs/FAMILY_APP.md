# 保護者アプリを作る人へ（成）

保護者向けの画面は、このリポジトリの**別のブランチ**で、別のアプリとして作ります（2026-11-20 の決定）。
職員向け（ブランチ `main`）が、データと決まりを持つ側です。このページは、保護者アプリの側から使えるものをまとめたものです。

## 決まっていること

| 項目 | 内容 |
|---|---|
| DB | 職員向けと**同じ DB** を使う（開発 `naru_app`・本番 `naru_app_prod`）。写真・書類の置き場所（`~/michinotenaru/media`）も同じ |
| 入り方 | **メールアドレスとパスワード**。アカウントは職員が作る（保護者は自分では登録できない） |
| LINE | 保留。いまの「公式 LINE から開く保護者ページ」（`/family/…`。`family/views.py` の `FamilyMixin`）は残してあるが、成の環境では LINE連携がオフなので開けない |
| 環境 | 開発 `https://st-naru-family.ai-labo.cloud/`・本番 `https://naru-family.ai-labo.cloud/`（Gunicorn 8050・置き場所 `~/michinotenarufamily`） |
| 配備 | Actions「Deploy (naru)」→ `app=family`。手順は `docs/DEPLOY_NARU.md`。`config.settings_family` で起動し、`SECRET_KEY` は職員向けと同じ値になる（配備が入れる）。**配備の前に、ブランチへ `main` を取り込む**（起動のスクリプト `deploy/venv-deploy.sh` が新しい版であること）。**本番の保護者向け（`target=prod`・`app=family`）は、保護者アプリの開発の担当も配備してよい**（2026-11-22、依頼者の許可。条件は下の「本番へ配備するとき」）。職員向けの本番は、依頼者（リポジトリの持ち主）が指示したときだけ |

## 守ってほしいこと

- **DB の形を変えるとき（migration）は、先に `main` に入れて職員向けを配備する。** 保護者向けの配備は migrate をしません。DB に無い migration がコードにあると、配備は止まります。
- **業務の決まりは、いまある関数を通す。** 表（モデル）に直接書き込むと、職員の画面とつじつまが合わなくなります（例：欠席連絡は職員が確認して初めて予定が欠席になる。利用日申請は来所予定と控えを同時に作る）。足りない関数があれば、`main` に足します。
- **ほかの家庭のものが見えないようにする。** 子どもは必ず `accounts.session_context()` が返した中から選ぶ。URL やフォームで受けた子どもの番号は、そのまま使わない。
- 保護者の画面の応答には、端末に残さない設定（`Cache-Control: no-store`）を付ける。いまの保護者ページは `family/views.py` の `_no_store` で付けています。
- 秘密の値・利用者の実データを、リポジトリ・Actions のログに入れない（このリポジトリは公開）。

## 本番へ配備するとき

保護者向けの本番（`https://naru-family.ai-labo.cloud/`）は、保護者アプリの開発の担当が、自分の作業の中で配備できます（そのつど依頼者に確かめなくてよい）。

1. `family` に最新の `main` を取り込み、テストを通す（`SECRET_KEY=test-key DEBUG=True python manage.py test --parallel 4`）。
2. Actions「Deploy (naru)」をブランチ `family` で、`target=dev`・`app=family` にして実行する。開発（`https://st-naru-family.ai-labo.cloud/`）で画面を確かめる。
3. 同じく `target=prod`・`app=family` で実行する。最後の「Health check」まで通ったことを確かめる。
4. Notion の更新履歴に 1 行残す。

できないこと：

- **職員向けの本番（`app=staff`）の配備**。依頼者が指示したときだけです。
- **DB の形を変える変更（migration）を含む配備**。先に `main` に入れて、職員向けの本番が配備されてからです（そうでないと、保護者向けの配備は止まります）。
- 切り戻すとき：Deploy (naru) の `ref` に、前のコミットの番号を入れて実行します（番号は Manage (naru) → `app=family`・`task=status` に出ます）。

## アカウントとひも付け（`family/accounts.py`）

- `FamilyAccount`：保護者 1 人に 1 つ。事業所ごとにメールアドレスが重ならない。パスワードはハッシュで持つ。
- `FamilyAccountLink`：アカウントと、保護者の記録（`beneficiaries.Guardian`。**子どもごとに 1 件**）の結びつき。
  - きょうだい＝同じメールアドレスの保護者の記録が、1 つのアカウントに付く。
  - 父と母＝それぞれのメールアドレスで、別のアカウント。
- 職員は、利用者の画面の「保護者」の欄で、アカウントを作る・パスワード設定のリンクを出す（メールで送る）・止める・外す。

保護者アプリの側で使う関数：

| 関数 | 使いどころ |
|---|---|
| `accounts.authenticate(email, password, facility=None)` | ログイン。合えば `FamilyAccount`、合わなければ `None`。5 回続けてまちがえると 15 分止まる。止めたアカウント・パスワード未設定・子どものいないアカウントは入れない |
| `accounts.check_setup_token(token)` | パスワード設定のリンクの合い言葉を確かめる。使えれば `FamilyAccount`、期限切れ（7 日）・使用ずみ・停止中は `None` |
| `accounts.set_password(account, raw)` | パスワードを決める。短い・ありふれている・メールアドレスに似ている ときは `ValidationError`（`e.messages` を画面に出す） |
| `accounts.session_context(account, child_id=None)` | 画面で使うひとそろい：`facility`・`children`（在籍中の子）・`child`（いま見ている子）・`guardian`（その子の保護者の記録）。使えないアカウントは `None` |

**保護者アプリで用意する画面**

- `GET/POST /account/setup/<token>/`：パスワードを決める画面。職員が出すリンク（`.env` の `FAMILY_APP_URL` ＋このパス）の行き先です。パスは `accounts.SETUP_PATH` で決めてあるので、変えるときは `main` も直します。
- ログイン・ログアウト。セッションには**アカウントの番号だけ**を入れ、画面のたびに `session_context()` を呼び直す（職員がアカウントを止めたら、次の操作から入れなくなるように）。
- パスワードを忘れたとき：いまは職員が「パスワード設定のリンク」を出し直す。保護者が自分で出し直す画面を作るなら、`accounts.send_setup_mail(account)` が使える（メールの設定がある環境だけ）。

```python
from family import accounts

account = accounts.authenticate(request.POST['email'], request.POST['password'])
if account is None:
    ...  # 「メールアドレスかパスワードが違います」（どちらが違うかは出さない）
request.session.cycle_key()
request.session['family_account'] = account.pk

# 画面のたびに
account = FamilyAccount.objects.select_related('facility').filter(pk=request.session.get('family_account')).first()
ctx = accounts.session_context(account, child_id=request.session.get('family_child'))
if ctx is None:
    ...  # ログイン画面へ
facility, child, guardian = ctx['facility'], ctx['child'], ctx['guardian']
```

## 画面ごとに使うもの

`guardian`・`child`・`facility` は、上の `session_context()` が返したものをそのまま渡します。送った人（保護者の名前とアカウント）は、関数の中で記録されます。

| 画面 | 読む | 書く | 決まり |
|---|---|---|---|
| 欠席連絡・その他の連絡 | `FamilyMessage`（`beneficiary=child`）。`is_confirmed` が職員の確認済み | `services.create_messages(facility, child, guardian, kind, dates, reason=, body=)` | 日にちは `services.clean_dates()` に通す。1 件＝1 人の 1 日。同じ子・同じ日の欠席は 1 件。欠席は**職員が確認した時**に来所予定が欠席になる |
| 連絡帳（成より） | `ContactBook`（`status=sent` だけ。下書きは見せない）。まだ読んでいないもの＝`services.unread_books(facility, child)` | `services.mark_book_read(book, guardian)` | 1 人 1 日 1 件。「開いたら確認済み」か「ボタンを押したら」かは、この関数を呼ぶ場所で決まる（成に確認中） |
| 連絡帳（保護者より） | `ParentNote`（`beneficiary=child`） | `services.add_parent_note(facility, child, guardian, day, body)` | 1 日に何件でも。書ける日は `services.note_writable(day)`（7 日前〜14 日先） |
| 予定のカレンダー | `yotei.month_view(facility, year, month, child=child, window=yotei.parent_window())`・`yotei.day_info(facility, day, child=child)` | — | 見るだけ（休業日・予定を入れるのは職員）。**必ず `child` を渡す**：予定には見せる相手（全員／グループ）があり、`child` を渡すと、その子に見せてよい予定だけになる（2026-11-22）。1 日に複数の予定がありうる：まとめたもの＝`event`（`title`・`label`・`detail`・`is_closed`。これまでの欄の名前のまま使える）、1 件ずつ＝`events`（`title`・`detail`）。保護者には見せる相手（`audience`）を出さない |
| 利用日申請 | 同上 | `yotei.apply_days(facility, child, days, via='parent', sender_name=services.sender_label(guardian), account=services.account_of(guardian))` | 日にちは `yotei.clean_days(values, yotei.parent_window())` に通す（あした〜92 日先）。承認なしで来所予定になる。休業日・予定のある日は入らない（戻り値の 2 つめ）。保護者の側からの取り消しは無い |
| お知らせ | `notices.feed(facility, child)`・`notices.unread_count(facility, child)` | `notices.mark_read(facility, child, guardian)` | 送信済みで、その子が宛先のものだけ。下書き・取り下げたものは出ない |
| 個別支援計画 | `family/views.py` の `PlanView` と同じ絞り込み（`current_step >= SupportPlan.STEP_CONSENT`、終了を除く）。欄の並びは `support_plans.sheet` | — | 担当者会議を終えた計画だけ。原案・アセスメントは出さない |
| 実績記録票のサイン | — | — | 未着手（方法は成と相談） |

関数の名前や引数は、使う前に `family/services.py`・`family/yotei.py`・`family/notices.py` で確かめてください。いまの保護者ページ（`family/views.py` の `FamilyMixin` を使う画面と `templates/family/`）が、同じ関数を使った動く見本です。

## まだ無いもの（要るときは `main` に足す）

- 保護者への知らせ（連絡帳・お知らせが届いたこと）：いまは LINE だけ。メール・アプリの通知は未定。
- 保護者が自分でメールアドレス・パスワードを変える画面のための関数。
- 利用日申請の取り消し。

## 手元で動かす

```bash
SECRET_KEY=test-key DEBUG=True python manage.py migrate
SECRET_KEY=test-key DEBUG=True python manage.py create_facility "見本" --admin naru --password '…' --preset houdei --demo
SECRET_KEY=test-key DEBUG=True python manage.py family_page --facility 1
SECRET_KEY=test-key DEBUG=True FAMILY_APP_URL=http://127.0.0.1:8001 python manage.py runserver
```

職員でログイン → 利用者 → 保護者の「編集」でメールアドレスを入れる → 「アカウントを作る」→「パスワード設定のリンク」。
テスト：`python manage.py test family.tests_accounts`（アカウント）・`python manage.py test family`（全体）。
