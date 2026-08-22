# Dance SaaS 🎵→💃

曲をアップロードすると、Seedance (Evolink API) が AI ダンス動画を自動生成する Web アプリ。
スマホ対応のフォームから曲をアップし、Seedance に生成ジョブを投げるところまでの骨格。

## セットアップ

```bash
cd ~/Desktop/dance_saas
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

- **API キーは各ユーザーがフォームに入力**します（EvoLink API キー）。サーバー側に固定キーは持ちません。
  - 入力キーはブラウザの localStorage に保存され、ジョブ実行中はサーバーのメモリ(`JOBS`)に task 単位で保持されます。
- 起動後 http://localhost:5055 、同じ Wi-Fi のスマホからは `http://<PCのIP>:5055` で開けます。

## フロー

1. スマホのフォームから曲(音声)をアップロード
2. `/upload` が Seedance に動画生成ジョブを投入し `task_id` を返す
3. ブラウザが `/status/<task_id>` を 4 秒ごとにポーリング
4. 完了したら `/finalize/<task_id>` が生成動画(無音)をDLし、曲を ffmpeg で合成
5. `/download/<task_id>` で完成 MP4 をダウンロード

依存: `ffmpeg`（`brew install ffmpeg`）。

## TODO（次工程）

- [ ] ビート解析して曲のテンポに合わせた prompt / duration 自動調整
- [ ] ジョブ永続化（今はメモリ上の `JOBS` dict）
- [ ] ユーザー認証・課金
```
