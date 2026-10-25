# 📋 League of Legends AI コーチングアプリ 詳細設計書 (Streamlit)

## 1. プロジェクト概要 (Project Overview)
- **目的**: プレイヤーの試合データを Riot API 経由で取得し、Gemini API を活用してカスタマイズ可能なAIコーチングおよび質疑応答チャットを提供する公開向けWebアプリケーション（製品名: LoL AI Coach）。
- **目標プラットフォーム**: Streamlit Community Cloud (完全無料枠で運用)
- **主要スタック**:
  - Web UI: `streamlit`
  - Riot API クライアント: `riotwatcher`
  - Gemini API クライアント: `google-genai`
  - 可視化: `plotly`

---

## 2. システム構成 & データフロー (System Architecture & Data Flow)

```
[ユーザー入力 (Riot ID / 部分名) / 環境設定 (API Keys)]
        │
        ├──> [SQLite (API Cache / Player Index)] <──> [Riot API (Asia/JP1)]
        │                                    │
        │                                    └──> [Match Data (JSON)]
        │                                    │
        │                                    ▼
        ├──> [st.session_state (キャッシュ)] ─┤
        │                                    ▼
        └──> [Gemini API (gemini-3.1-flash-lite / Interactions API)] <── [指示書 (System Prompt)]
                     │
                     ▼
             [コーチング回答 / チャット応答]
```

---

## 3. 画面・UI コンポーネント設計 (UI Component Design)

### 3.1 入力とキー管理（サイドバーなし）
1. Riot ID（`GameName#TAG`）またはプレイヤー名の一部をメイン画面上部の入力欄に入力する。
2. APIキーは利用者に入力させない。Riot APIキーと Gemini APIキーは環境変数または Streamlit secrets からサーバー側で読み込む。公開アプリで他人のキーを露出させないため、画面に入力欄は設けない。キーがない場合は日本語のエラーを表示して停止する。
3. キュータイプ（「ソロランク」「ノーマル」「すべて」）はメイン画面の入力欄の隣で選択する。
4. **リージョン**
   - UIでは選択させず、日本サーバー `JP1` に固定する。リージョンから地域ルートへの対応表は保持する。

### 3.2 メインエリア

#### 【エリア 0】ヘッダー
- アプリ名「⚔️ LoL AI Coach」とサブタイトルを1行にまとめ、利用上の注意（Riot Games 非公式である旨）を小さく添える。
- 検索欄、キュータイプ、同期ボタンは1行に並べ、ダッシュボードが1画面に収まるように縦の余白を詰める。
- 専用のテーマは `.streamlit/config.toml` のダークテーマ設定と、ヘッダー用の CSS で統一する。

#### 【エリア A】戦績取得
- **取得コントロール**:
  - 取得数は直近20試合で固定する。
  - 対象キューは選択に応じて、ソロランク（420）、ノーマル（400/430/490）、またはすべて（400/420/430/440/490）とする。ARAMや期間限定モードは含めない。
  - `st.button("最新の戦績を同期")`:
    - Riot API から `Account-v1` で `PUUID` を取得
    - 選択したキューに応じて `Match-v5` から試合 ID を取得し、直近順に最大20試合の詳細を取得
    - 取得結果を `st.session_state['matches_data']` に格納

#### 【エリア B】直近の戦績ダッシュボード (2×2 パネル)
`build_recent_stats` の集計結果を、固定高さ（`DASHBOARD_PANEL_HEIGHT` = 300px）の `st.container(border=True)` 4 枚で 2×2 に表示する。左列を狭く、右列を広くし、試合一覧が1行で読めるようにする。
- **パネル1 戦績サマリー**: 試合数、勝率、平均KDA、平均CS/分、よく使うチャンピオン、ポジション別の試合数と勝率を `st.metric` で表示。
- **パネル2 KDA推移**: 試合ごとの KDA を古い順に棒グラフで表示（勝ち/負けで色分け）。
- **パネル3 チャンピオン別の成績**: 上位8チャンピオンの試合数、勝率、KDA を表で表示。
- **パネル4 試合一覧**: 勝敗、チャンピオン、KDA、CS/分、試合時間を行単位で表示し、パネル内でスクロールできる。各行の「🔍 コーチングを受ける」で詳細を選択する。
- ボタンラベルは幅の狭いパネルに収めるため「🔍 コーチングを受ける」とする。

#### 【エリア C】試合詳細 & 分析 (Match Detail)
- 3つのタブでスタット詳細、AIコーチング、試合チャットを表示する。スタット詳細にはチャンピオン画像、KDA、CS/分、ダメージ、視界、ゴールド、両チームのチャンピオン、ビルドを表示する。
- **AIコーチング生成**:
  - タイムラインと統計を Gemini に送り、勝敗を分けた場面、ソロランクでの強み・弱み、次回の宿題1つ、おすすめチャンピオンを日本語で出力する。
  - 時間軸で確認できない判断は断定せず、1試合だけではプレイ傾向を判断できない場合はその限界を明記する。

#### 【エリア D】コーチとの個別チャット
- `st.chat_message` と `st.chat_input` を使用
- 過去の対話履歴を `st.session_state['chat_history']` に保持
- プロンプトには常に「分析対象の試合データ」を背景コンテキストとして埋め込んで送信
- 既存の会話型質問応答を維持する。

---

## 4. データ構造 (Data Schemas)

### 4.1 Gemini 送信用 Match JSON サマリー (軽量化版)
全データを送るとトークン数を圧迫するため、以下の抽出データに整形して送信する。

```json
{
  "gameDurationMinutes": 28.5,
  "win": false,
  "targetPlayer": {
    "champion": "Aatrox",
    "role": "TOP",
    "kills": 3,
    "deaths": 7,
    "assists": 2,
    "kda": "0.71",
    "cs": 180,
    "csPerMin": 6.3,
    "goldEarned": 9800,
    "damageDealt": 14200,
    "visionScore": 12,
    "items": ["Goresky", "Plated Steelcaps", "Sterak's Gage"]
  },
  "laneOpponent": {
    "champion": "Darius",
    "kills": 8,
    "deaths": 2,
    "assists": 5,
    "goldEarned": 12400,
    "damageDealt": 18500
  },
  "teamStats": {
    "totalKills": 15,
    "totalGold": 45000,
    "dragons": 1,
    "barons": 0
  }
}
```

---

## 5. エラーハンドリング & 運用ルール (Error Handling)

1. **Riot API 401/403 Error (キー期限切れ / 無効)**
   - `st.error("Riot API Key が無効、または有効期限(24時間)が切れています。Developer Portal で新しいキーを取得してください。")`
2. **Riot API 404 Error (IDが存在しない)**
   - `st.error("指定された Riot ID (GameName#TAG) が見つかりませんでした。表記を確認してください。")`
3. **Gemini API Key Error**
   - UIで入力を求めず、`GEMINI_API_KEY` の環境変数または Streamlit secrets の設定を案内する。
4. **セッションキャッシュ設計**
   - 試合データやチャット履歴は `st.session_state` に保持し、UIパーツ操作ごとに無駄な API リクエストが発生しないように防ガードする。

---

## 6. 実装フェーズ案 (Implementation Steps for Copilot)

1. **Phase 1: API 接続検証ロジックの構築**
   - `riotwatcher` で PUUID 取得 ➔ 試合 ID 取得 ➔ 試合詳細データ取得の関数作成
2. **Phase 2: Streamlit UI レイアウト構築**
   - タイトル直下のアカウント検索フォームとメインエリアのタブ/カラム設計
3. **Phase 3: データ加工 & 可視化**
   - Riot API JSON から自分のデータを抽出し、Pandas / Plotly でグラフ化
4. **Phase 4: Gemini API 連携 & プロンプト組み込み**
   - `google-genai` パッケージを用いて Gemini（`MODEL_NAME = "gemini-3.1-flash-lite"`）と Interactions API で通信する関数の実装
   - 試合タイムラインに基づく簡潔なティーモ風コーチングの実装
5. **Phase 5: チャット機能 & 仕上げ**
   - `st.chat_message` を使った対話 UI の組み込みとエラーハンドリングの追加
```

---

### 次のステップ
1. 上記の `lol_coaching_app_spec.md` ファイルの内容をそのまま **GitHub Copilot** や **Cursor** などのAIエージェントに入力し、「この設計書に従って Streamlit アプリ（`app.py`）を実装してください」と指示を出してみてください。
2. ライブラリは `pip install streamlit google-genai riotwatcher pandas plotly` でまとめてインストール可能です。

実装中につまずいた部分やコードの書き方で疑問が出たらいつでも聞いてください！