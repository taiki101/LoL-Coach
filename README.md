# ⚔️ LoL AI Coach

ソロランクで勝てないプレイヤーのための専属AIコーチです。Riot ID を入力するだけで直近の戦績を同期し、試合ごとの敗因・勝因と次の一手を Gemini がコーチングします。試合の振り返りは、そのままチャットで深掘りできます。

League of Legends の日本サーバー（JP1）に対応しています。ARAM・期間限定モードは対象外です。

## 主な機能

- **2×2 ダッシュボード**: 1画面に4つのパネルを並べ、直近の戦績を一目で確認できます。
  - **戦績サマリー**: 勝率・平均KDA・CS/min・よく使うチャンピオンとポジションを表示します。
  - **KDA推移**: 試合ごとのKDAを古い順に棒グラフで表示し、勝敗を色分けします。
  - **チャンピオン別の成績**: 使用回数の多い上位8チャンピオンの勝率とKDAを表示します。
  - **試合一覧**: 勝敗、チャンピオン、KDA、CS/min、試合時間を一覧表示し、各行から詳細を選べます。
- **キュータイプ切り替え**: ソロランク、ノーマル、すべてから選んで同期できます。
- **📊 戦績データ・比較**: KDA、CS、与ダメージ、ゴールド推移をチームと比べながら確認できます。
- **🎯 専属AIコーチング**: 敗因・勝因の解剖、ソロランクの勝ちパターン診断、次回すぐ試せる宿題1つ、おすすめチャンピオンを提案します。
- **💬 コーチと1対1で相談**: 試合の場面や対面について、AIコーチと自由に質問・議論できます。

> 注意: 本アプリは Riot Games によって承認・運営されているものではありません。Riot Games の公式サービスではありません。

## 利用者向けの使い方

1. Riot ID（例: `GameName#TAG`）を入力します。
2. キュータイプを選び、「🔄 最新の戦績を同期」を押します。
3. 試合一覧の「🔍 コーチングを受ける」を押すと、戦績比較、AIコーチング、チャットを利用できます。

Riot ID の名前の一部だけを入力すると、同期済みの試合に登場したプレイヤーから検索します。Riot API に名前の部分一致検索はないため、一度も同期していないプレイヤーは `GameName#TAG` の完全な形式で入力してください。

APIキーは利用者が入力する必要はありません。運営者がサーバー側に設定します。

## Step 1: 自分のPC（ローカル）でテスト実行する

まずはローカルPCでアプリが正しく動作するか確認します。VS Code のターミナルまたは PowerShell で、プロジェクトのフォルダに移動して以下を実行してください。

1. サーバー側に Riot APIキーと Gemini APIキーを設定します。キーはアプリの画面やリポジトリに書かず、次のいずれかで設定してください。

   - プロジェクト直下の `.streamlit/secrets.toml` に設定します（このファイルは Git 管理対象外です）。

     ```toml
     RIOT_API_KEY = "自分のRiot API Key"
     GEMINI_API_KEY = "自分のGemini API Key"
     ```

   - または、PowerShell の現在のターミナルで環境変数を設定します。

     ```powershell
     $env:RIOT_API_KEY = "自分のRiot API Key"
     $env:GEMINI_API_KEY = "自分のGemini API Key"
     ```

   Streamlit Community Cloud では、アプリの Settings → Secrets に `RIOT_API_KEY` と `GEMINI_API_KEY` を設定してください。

2. Python 3.15 を用意し、プロジェクト直下に仮想環境を作成します。

   ```powershell
   py -V:3.15 -m venv .venv
   ```

3. 仮想環境に必要なライブラリをインストールします。

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   `requirements.txt` には Streamlit、Gemini、Riot API、データ処理・可視化に必要なライブラリが含まれています。

4. `app.py` があるフォルダでアプリを起動します。

   ```powershell
   .\.venv\Scripts\python.exe -m streamlit run app.py
   ```

5. ブラウザが自動で開かない場合は、ターミナルに表示される `http://localhost:8501` にアクセスします。

### SQLite キャッシュとレート制限

Riot API の応答と、取得済み試合から見つかったプレイヤーを `riot_cache.sqlite3` に保存します。同じデータの再取得を減らし、名前の一部から保存済みプレイヤーを検索できます。キャッシュはアカウント情報を24時間、試合 ID リストを5分、試合詳細とタイムラインを30日間利用します。

API キーごとに SQLite でリクエストを記録し、最大20回/秒・100回/2分を超えないよう待機します。キーはサーバーで1つだけ使うため、利用者が増えてもこの上限を全員で共有します。ローカル実行では再起動後もキャッシュを使えます。Streamlit Community Cloud ではインスタンスの再作成時にローカル SQLite ファイルが消える場合があります。

Riot API の開発者キーは有効期限が短い場合があります。取得エラーが続くときは Riot Developer Portal でキーを確認してください。

## Step 2: Streamlit Community Cloud に公開する

1. コードを GitHub のリポジトリに push します。`.streamlit/secrets.toml` と `riot_cache.sqlite3` は push しないでください（`.gitignore` で除外済みです）。
2. [Streamlit Community Cloud](https://share.streamlit.io/) に GitHub アカウントでログインし、「Create app」を選びます。
3. Repository に `taiki101/LoL-Coach`、Branch に公開するブランチ、Main file path に `app.py` を指定します。Python のバージョンは 3.12 または 3.13 を選ぶと安全です。
4. 「Advanced settings」の Secrets に次の内容を入力します。
   ```toml
   RIOT_API_KEY = "RGAPI-..."
   GEMINI_API_KEY = "..."
   ```
5. 「Deploy」を押すと公開されます。`requirements.txt` のバージョンが固定されているため、同じ依存関係で再現できます。

公開前に、Riot の本番キー申請（開発者キーは公開利用に使えません）と、Gemini の利用上限・予算アラートを設定してください。

