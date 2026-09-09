# headroom-for-mobile-and-web

[English README](./README.md)

[Headroom](https://github.com/headroomlabs-ai/headroom) の圧縮ライブラリを
単なるPython関数として呼び出し、大きなファイルをコンテキストに入れる前に
縮めるAgent Skill。プロキシもMCPサーバーもAPIキーも不要。

サンドボックス型のチャット環境（Claudeのモバイル版・Web版、ChatGPT Work、
その他シェルを持つエージェント）向け。これらの環境ではHeadroom本来の
デプロイ方式が動かない。

## 課題

Headroomは通常、HTTPプロキシかMCPサーバーとして常駐させる。サンドボックス型の
チャット環境にはどちらも置けない。常駐プロセスが無く、ファイルシステムは
セッションごとに消える。結果として、Headroomの圧縮を「可逆」にしている
CCRストア — 要約では足りないときにモデルが原本を取り寄せられる仕組み — の
置き場所が無い。

一方で、圧縮パイプライン自体はただのPython関数であり、サンドボックスには
シェルとファイルシステムがある。

## アプローチ

**圧縮 → 保持 → 取り出し。** ファイルを圧縮し、圧縮版を読み、特定の値が
必要になった時点で保存済みの原本から正確な内容を取り出す。原本は
サンドボックスの外に出ないので、取り出しは常に厳密。ディスク上のファイルで
CCRのパターンを再構成している。

その上で、圧縮は毎回**検証**される。Headroomは内容に応じて別々の圧縮器へ
振り分ける。可逆に再構成するものもあれば、行を間引くものもある。圧縮率からは
どちらが走ったか判別できない。このSkillは原本から希少値をサンプリングして
残存を確認することでそれを測定し、基準を満たさなければ自らの結果を却下する。

## リポジトリ構成

```
headroom-for-mobile-and-web/
├── README.md           英語版
├── README.ja.md        このファイル
├── LICENSE
└── skill/
    └── headroom-for-mobile-and-web/   実際にインストールするSkill本体
        ├── SKILL.md
        ├── scripts/hr.py
        └── references/tuning.md
```

## 導入

`skill/headroom-for-mobile-and-web/` をエージェントのskillsディレクトリに
配置する。対応環境ではこのディレクトリをそのまま `.skill` バンドルとして
パッケージ化しアップロードしてもよい。
`headroom-ai` は初回使用時に自動インストールされる（ネットワーク必須）。

## 使い方

```bash
S=path/to/headroom-for-mobile-and-web/scripts/hr.py

python3 $S scan .                        # 何が大きく、何を圧縮すべきか
python3 $S compress data/response.json   # 削減量と忠実度を測定し、両方を保存
python3 $S retrieve <id> --grep ERROR    # 原本から正確な行を取り出す
python3 $S stats                         # 累計削減量
```

```
$ python3 $S compress quotes.json
id           0a2d4b17d7
source       quotes.json
tokens       10,980 -> 4,699   (saved 6,281, 57.2%)
fidelity     100.0% of 300 sampled values retained  [lossless]
transforms   router:smart_crusher:0.35
```

全コマンドが `--json` に対応（サブコマンドの前後どちらでも指定可）。

## 結果の読み方

圧縮率より `fidelity`（忠実度）が重要。レコード単位のID、一度きりの
エラー文字列、外れ値といった希少値をサンプリングし、圧縮後も残っているかを
確認する。これらは間引き型の圧縮器が最初に壊すもの。

| 判定 | 意味 |
|---|---|
| `lossless` | サンプルした値がすべて残存。圧縮版をそのまま読んでよい |
| `near-lossless` | 95%以上残存。傾向の把握には十分。個別レコードを引用する前に取り出すこと |
| `lossy` | 95%未満。個別レコードの推論には使わないこと |
| `no-op` | 圧縮されなかった。原本を読むこと |

`--min-ratio`（既定15%）または `--min-fidelity`（既定95%）を下回る結果は
自動的に却下される。却下は失敗ではなく有用な回答で、「原本を直接読め」を意味する。

## 圧縮が効くもの

| 内容 | 典型的な結果 |
|---|---|
| オブジェクト配列のJSON、NDJSON、表形式CSV | 40〜60%、多くの場合可逆 |
| 繰り返しの多いログ | 15〜30%、採用閾値を下回ることも多い |
| ソースコード | no-op — 該当範囲を直接読むほうがよい |
| 散文 | no-op |

ソースコードがno-opなのは意図的な設計であり、対応漏れではない。HeadroomのAST圧縮器は
関数・メソッドの中身を削って骨格だけを抽出するもので、安全なサイズ削減ではない。
有効化する内部スイッチ自体は存在するが、このSkillは意図的に使用していない。
そのスイッチに触れる前に [`references/tuning.md`](./references/tuning.md) を参照。

## コマンド

| コマンド | 用途 |
|---|---|
| `scan PATH...` | ファイルごとのトークン推定と、圧縮すべきかの判定 |
| `compress FILE` | 圧縮し、削減量と忠実度を測定、原本と圧縮版を保存 |
| `retrieve REF` | 保存済み原本から正確な内容を取得（`--grep` / `--lines` / `--json-index`） |
| `stats` | ワークスペースの累計削減量 |
| `ensure` | `headroom-ai` を明示的にインストール |

取り出し時、圧縮された1行JSONは先に整形されるため、行番号とgrepが機能する。
長い行は切り詰められ、マッチ件数にも上限があるので、広すぎるパターンが
本来守るはずのコンテキストを溢れさせることはない。

## 動作要件

Python 3.9以上、初回使用時のみネットワーク。APIキー・アカウント・外部サービスは
不要。`headroom-ai` は完全にローカルで動作し、サンドボックスの外へは何も出ない。

## ライセンス

MIT。[LICENSE](./LICENSE) を参照。

Headroom本体はHeadroom Labsによる別プロジェクトで、Apache-2.0で配布されている。
このSkillはその公開Python APIを呼び出しているだけ。
