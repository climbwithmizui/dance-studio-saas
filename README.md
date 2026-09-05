# Dance Studio

キャラクター画像から Seedance（EvoLink API）でダンス動画を生成するFlaskアプリです。

- 参照動画なし: オリジナルダンス生成。480p / 720p / 1080pを選択でき、曲を指定した場合は完成動画へ合成します。
- 参照動画あり: 振り付けを参照する720p生成。完成動画は著作権への配慮から無音です。
- Dance Studioは月額1,980円。EvoLinkのアカウントと生成クレジットは利用者が別途用意します。

## ローカルセットアップ

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

ffmpeg / ffprobeが必要です。Dockerイメージにはインストール済みです。

## 環境変数

必要な項目は `.env.example` を参照してください。StripeのPrice IDも `STRIPE_PRICE_ID` として環境変数で管理します。秘密値をGitへコミットしないでください。

利用者が画面へ入力したEvoLink APIキーはブラウザのlocalStorageへ保存され、生成中のみサーバーのメモリへ保持されます。生成完了または失敗後、サーバー上のキーと一時素材を破棄します。

## ヘルプ・法的ページ

- `/guide`: Dance Studio使い方ガイド（ログイン＋有効なサブスクが必要）
- `/guide/evolink-api-key`: APIキー取得ガイド（ログイン＋有効なサブスクが必要）
- `/terms`: 利用規約（公開）
- `/privacy`: プライバシーポリシー（公開）
- `/tokushoho`: 特定商取引法に基づく表記（公開）

未契約者向け画面では外部AIサービスの固有名・APIキー取得手順・クレジット目安を表示しません。法的ページは購入前の確認に必要なため公開し、外部サービス名は一般化しています。

## 生成フロー

1. 利用者がAPIキー、画像、任意の参照動画または曲を選択
2. 生成モード、解像度、クレジット目安、音声仕様を最終確認
3. `/upload` が形式・容量・参照動画尺を検査してEvoLinkへ生成ジョブを投入
4. `/status/<task_id>` で本人のジョブだけを確認
5. `/finalize/<task_id>` が完成動画を取得し、オリジナルダンスで曲がある場合だけffmpegで合成
6. `/download/<task_id>` で本人だけが完成MP4をダウンロード

## テスト

外部APIへ実通信せずに実行できます。

```bash
python -m unittest discover -s tests -v
```

## 現在の制約

- 生成ジョブはメモリ上に保持するため、サーバー再起動後は再開できません。
- Renderの一時ストレージに置いた完成動画は永続保存されません。
- 生成履歴と再ダウンロードを実装するには、保存期間・削除方法・外部ストレージを先に決める必要があります。
