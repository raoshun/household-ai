# Household AI
## 家庭内ルール管理・判断支援AIチャットアプリ 開発仕様書

- バージョン：1.0
- 作成日：2026-11-07
- ステータス：開発開始可能
- 開発方式：AIコーディングエージェントによる段階的実装
- 主要技術：GitHub Copilot SDK / Python / FastAPI / React / SQLite

---

# 1. プロジェクト概要

## 1.1 目的

家庭内のルール、方針、判断基準、例外条件などを体系的に蓄積し、AIがそれらを参照して家庭内の相談や意思決定を支援するチャットアプリケーションを開発する。

単なる会話型AIではなく、家庭内の知識を継続的に蓄積・更新できるナレッジベース型エージェントを目指す。

## 1.2 解決したい課題

一般的なチャットAIには以下の問題がある。

- 過去の会話で決めたことを正確に再利用できない。
- 家庭内のルールと一般論が混同される。
- ルールの例外や適用条件が曖昧になる。
- 家族間で決めたことが会話履歴に埋没する。
- AIが独自にルールを解釈・変更してしまう。
- どのような根拠で回答したのか確認しにくい。

本アプリでは、家庭内ルールを独立した構造化データとして管理し、AIの判断材料として利用することでこれらを解決する。

## 1.3 基本コンセプト

以下の4つを基本原則とする。

1. **ルールと会話を分離する。**
2. **AIは登録済みルールを参照して判断する。**
3. **新しいルールはAIが提案し、人間が承認する。**
4. **AIの判断根拠を人間がいつでも確認できる。**

## 1.4 想定利用者

家庭内の複数のメンバー。

初期段階では夫婦による利用を想定するが、将来的に子どもなどを含む複数ユーザーへの拡張を考慮する。

---

# 2. システム構成

## 2.1 採用技術

| 分類 | 技術 | 用途 |
|---|---|---|
| AI Agent | GitHub Copilot SDK | エージェント実行 |
| Backend | Python / FastAPI | アプリケーションAPI |
| Frontend | React / TypeScript | チャット・管理画面 |
| UI | Tailwind CSS | UIデザイン |
| Database | SQLite | 会話・ルール管理 |
| Rule Storage | Markdown + YAML | ルールの正本 |
| Search | SQLite FTS5 | 全文検索 |
| Validation | Pydantic | データ検証 |
| Container | Docker Compose | デプロイ |
| Test | pytest / Vitest | テスト |

依存ライブラリは実装時点の安定版を採用し、バージョンを固定する。

## 2.2 アーキテクチャ

```text
                    Browser
                       |
                       |
              React / TypeScript
                       |
                  REST / SSE
                       |
              FastAPI Application
                       |
          +------------+------------+
          |            |            |
      Chat Service  Rule Service  History Service
          |            |            |
          |            |            |
          +------------+------------+
                       |
                Agent Runtime
                       |
                GitHub Copilot SDK
                       |
                Household Agent
                       |
          +------------+------------+
          |            |            |
        Skills    Custom Tools   Session
          |            |
          |            |
     SKILL.md     Rule Service
                       |
             +---------+---------+
             |                   |
        SQLite DB          Markdown Files
             |                   |
        Chat History        Household Rules
        Approval State      Rule Versions
        Audit Logs           Rule Documents
```

## 2.3 設計上の重要事項

Copilot SDKへの依存をアプリケーション全体に広げない。

AI実行部分を抽象化し、将来別のLLMやエージェント基盤に切り替えられる構成にする。

ただし、初期実装ではGitHub Copilot SDKを正式な実行基盤とする。

---

# 3. ディレクトリ構成

以下を標準構成とする。

```text
household-ai/
│
├── docker-compose.yml
├── .env.example
├── README.md
│
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   │
│   └── app/
│       ├── main.py
│       ├── config.py
│       │
│       ├── api/
│       │   ├── chat.py
│       │   ├── rules.py
│       │   ├── approvals.py
│       │   └── history.py
│       │
│       ├── services/
│       │   ├── chat_service.py
│       │   ├── rule_service.py
│       │   ├── search_service.py
│       │   ├── approval_service.py
│       │   └── audit_service.py
│       │
│       ├── agent/
│       │   ├── runtime.py
│       │   ├── provider.py
│       │   ├── session_manager.py
│       │   ├── tools.py
│       │   ├── hooks.py
│       │   └── prompts.py
│       │
│       ├── models/
│       │   ├── rule.py
│       │   ├── conversation.py
│       │   └── approval.py
│       │
│       ├── repositories/
│       │   ├── rule_repository.py
│       │   ├── chat_repository.py
│       │   └── approval_repository.py
│       │
│       └── db/
│           ├── database.py
│           └── migrations/
│
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   └── src/
│       ├── App.tsx
│       ├── pages/
│       │   ├── ChatPage.tsx
│       │   ├── RulesPage.tsx
│       │   ├── RuleDetailPage.tsx
│       │   ├── PendingPage.tsx
│       │   └── HistoryPage.tsx
│       │
│       ├── components/
│       │   ├── ChatMessage.tsx
│       │   ├── RuleCard.tsx
│       │   ├── RuleEditor.tsx
│       │   ├── RuleReference.tsx
│       │   └── ApprovalDialog.tsx
│       │
│       └── api/
│           └── client.ts
│
├── data/
│   ├── household.db
│   └── rules/
│       ├── parenting/
│       ├── education/
│       ├── health/
│       ├── lifestyle/
│       ├── finance/
│       └── general/
│
├── skills/
│   ├── household-advisor/
│   │   └── SKILL.md
│   │
│   ├── rule-management/
│   │   └── SKILL.md
│   │
│   └── rule-auditor/
│       └── SKILL.md
│
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/
```

初期段階では `household-advisor` と `rule-management` の2つを実装する。

`rule-auditor` は将来拡張用とする。

---

# 4. 家庭内ルール管理

## 4.1 ルールの基本方針

家庭内ルールはMarkdownファイルとして保存する。

YAML Front Matterで構造化情報を保持し、本文は自然言語で記述する。

ルールの正本はMarkdownファイルとする。

SQLiteには検索用インデックス、承認状態、履歴、メタデータを保持する。

SQLiteとMarkdownの間でデータの不整合が発生した場合に備え、再インデックス機能を実装する。

## 4.2 ルールのデータ構造

```yaml
---
id: parenting-001
title: お菓子の与え方
category: parenting

status: active
version: 1

rule_type: principle
priority: normal

applicable_to:
  - children

tags:
  - food
  - parenting

created_at: 2026-11-07T10:00:00+09:00
updated_at: 2026-11-07T10:00:00+09:00

created_by: user
approved_by: user
---

# 基本方針

食事の前にお菓子を与えない。

# 適用条件

通常の家庭内でのおやつ。

# 例外条件

外出時など、特別な事情がある場合は柔軟に対応する。

# 判断基準

食事への影響を優先して考える。
```

## 4.3 ルールの属性

| 属性 | 説明 |
|---|---|
| id | 一意の識別子 |
| title | ルール名 |
| category | カテゴリ |
| status | ステータス |
| version | バージョン |
| rule_type | ルールの性質 |
| priority | 優先順位 |
| applicable_to | 適用対象 |
| tags | 検索タグ |
| created_at | 作成日時 |
| updated_at | 更新日時 |
| created_by | 作成者 |
| approved_by | 承認者 |

## 4.4 ルールの種類

以下の種類を定義する。

| 種類 | 説明 |
|---|---|
| strict | 原則として必ず守る決まり |
| principle | 基本的な方針 |
| preference | 家族の好み・希望 |
| guideline | 判断時の参考基準 |
| exception | 特定条件における例外 |

AIはルールの種類を区別して回答すること。

例えば、preferenceをstrictとして扱ってはならない。

## 4.5 ステータス

| ステータス | 説明 |
|---|---|
| draft | 作成中 |
| pending_approval | 承認待ち |
| active | 有効 |
| rejected | 却下 |
| archived | 廃止・保管 |

AIが提案したルールは必ずpending_approvalで保存する。

activeへの変更は人間の承認操作のみで実行する。

## 4.6 バージョン管理

ルールの更新時には既存ファイルを上書きせず、変更履歴を残す。

例：

```text
data/rules/parenting/
    parenting-001/
        v1.md
        v2.md
        v3.md
```

最新版への参照を管理する。

各変更について以下を記録する。

- 変更前の内容
- 変更後の内容
- 変更理由
- 変更者
- 承認者
- 変更日時

過去のバージョンを参照・復元できるようにする。

---

# 5. Agent Skills

## 5.1 基本方針

Agent SkillsはAIに判断方法や作業手順を教えるために使用する。

家庭内ルールそのものはSkillsに直接記述せず、Rule Serviceから取得する。

Skillsとルールを独立して管理することで、判断ロジックと家庭内の決定事項を分離する。

## 5.2 household-advisor

`skills/household-advisor/SKILL.md`

```markdown
---
name: household-advisor
description: 家庭内ルールを参照し、家庭内の相談や意思決定を支援する
---

# Household Advisor

あなたは家庭内ルールに基づいて相談に回答するAIアシスタントです。

## 基本原則

1. 家庭内ルールを重要な判断材料として扱う。
2. 一般論と家庭内で決めたことを区別する。
3. ルールにない事項を勝手に決定しない。
4. ルールの例外条件を必ず確認する。
5. 複数のルールが矛盾する場合は明示する。
6. 人間の最終的な意思決定を尊重する。

## 判断手順

1. ユーザーの相談内容を理解する。
2. 関連するルールを検索する。
3. 該当ルールの全文を取得する。
4. 適用条件を確認する。
5. 例外条件を確認する。
6. ルールに基づいて判断する。
7. 根拠となったルールIDを提示する。

## ルールが存在しない場合

家庭内ルールが登録されていない場合は、その旨を明示する。

一般的な知識に基づく参考意見を述べてもよいが、家庭内の決定事項として扱ってはならない。

## 回答形式

- 結論
- 判断理由
- 適用した家庭内ルール
- 例外・注意事項
- 必要に応じた提案
```

## 5.3 rule-management

`skills/rule-management/SKILL.md`

```markdown
---
name: rule-management
description: 会話から家庭内ルール候補を抽出・整理する
---

# Rule Management

会話内容から新しい家庭内ルールの候補を抽出する。

## 基本原則

- 単なる質問や仮定をルールとして登録しない。
- 明確な意思表示と一時的な発言を区別する。
- 既存ルールとの重複を確認する。
- 既存ルールと矛盾する場合は明示する。
- 適用条件と例外条件を可能な限り明確化する。
- 曖昧な場合はユーザーに確認する。

## 登録手順

1. ルール候補を抽出する。
2. 既存ルールを検索する。
3. 重複・矛盾を確認する。
4. 不足情報を確認する。
5. 構造化されたルール候補を作成する。
6. pending_approvalとして保存する。
7. 人間の承認を待つ。

## 禁止事項

AI自身の判断だけでactiveに変更してはならない。
```

---

# 6. AI Agent設計

## 6.1 Household Advisor Agent

初期段階では単一のAgentとする。

Copilot SDKのセッションを利用し、会話単位でコンテキストを管理する。

Agentには以下の機能を与える。

- 家庭内ルールの検索
- ルール詳細の取得
- ルールの適用・解釈
- ルール候補の作成
- 既存ルールとの比較
- 判断根拠の提示

## 6.2 Custom Tools

Copilot SDKのCustom Toolsを利用する。

以下のツールを実装する。

### search_rules

家庭内ルールを検索する。

入力：

```json
{
  "query": "夕食とお菓子",
  "category": "parenting",
  "status": "active",
  "limit": 10
}
```

出力：

```json
{
  "results": [
    {
      "id": "parenting-001",
      "title": "お菓子の与え方",
      "category": "parenting",
      "status": "active",
      "summary": "食事前にお菓子を与えない"
    }
  ]
}
```

### get_rule

ルールの詳細を取得する。

入力：

```json
{
  "rule_id": "parenting-001"
}
```

出力：

ルールの全文、バージョン、適用条件、例外条件を返す。

### propose_rule

新しいルールを提案する。

入力：

```json
{
  "title": "夕食を残した場合のデザート",
  "category": "parenting",
  "rule_type": "principle",
  "applicable_to": ["children"],
  "content": {
    "basic_rule": "夕食を残した場合はデザートを与えない",
    "exceptions": [
      "体調不良の場合"
    ]
  },
  "reason": "会話から抽出したルール候補"
}
```

出力：

```json
{
  "success": true,
  "rule_id": "parenting-002",
  "status": "pending_approval"
}
```

### evaluate_rule

既存ルールと新しい候補を比較する。

入力：

```json
{
  "rule_candidate": {
    "title": "夕食を残した場合のデザート",
    "content": "夕食を残した場合はデザートを与えない"
  }
}
```

出力：

- 関連ルール
- 重複の可能性
- 矛盾の可能性
- 不足している情報
- 確認が必要な事項

## 6.3 Tool実行の権限制御

ルールの変更を伴う操作については、必ずアプリケーション側で権限を検証する。

AIのプロンプトだけで制御してはならない。

| 操作 | AIからの実行 |
|---|---|
| ルール検索 | 許可 |
| ルール取得 | 許可 |
| ルール候補作成 | 許可 |
| ルールの直接更新 | 禁止 |
| activeへの変更 | 禁止 |
| ルール削除 | 禁止 |
| 承認 | 人間の操作のみ |

AIによるルール候補の作成は許可するが、承認・反映はWeb API経由の人間の操作に限定する。

## 6.4 Hooks

Copilot SDKのHooksを利用し、必要な制御を実装する。

優先するHooks：

- on_session_start
- on_pre_tool_use
- on_post_tool_use
- on_user_prompt_submitted
- on_session_end
- on_error_occurred

実際の名称・引数・戻り値は導入SDKの公式APIに合わせる。

主な用途：

- セッション開始時のコンテキスト設定
- 不許可ツールの実行拒否
- ツール実行履歴の記録
- 不正な入力の検証
- エラー処理

Hooksを唯一のセキュリティ境界にしてはならない。最終的な権限検証はService層で実施する。

---

# 7. LLMプロバイダーの抽象化

## 7.1 基本方針

初期実装ではGitHub Copilot SDKを使用する。

将来的にOpenAI API、Anthropic API、Ollamaなどを利用できるようにする。

ただし、すべてのプロバイダーが同じツール実行機能やセッション機能を提供するとは限らない。

そのため、共通インターフェースを定義し、プロバイダー固有の機能を分離する。

## 7.2 インターフェース

概念上、以下の機能を抽象化する。

```python
class AgentProvider:

    async def create_session(self, config):
        ...

    async def send_message(self, session_id, message):
        ...

    async def stream_message(self, session_id, message):
        ...

    async def close_session(self, session_id):
        ...

    async def health_check(self):
        ...
```

Copilot SDKの実際のAPIをそのまま抽象インターフェースに流用せず、アプリケーション側で必要な機能を定義する。

## 7.3 プロバイダー設定

環境変数または設定ファイルで切り替えられるようにする。

```yaml
ai:
  provider: copilot

  copilot:
    model: null

  openai:
    model: null

  anthropic:
    model: null

  ollama:
    base_url: http://host.docker.internal:11434
    model: null
```

APIキーなどの認証情報は設定ファイルに直接記述しない。

初期実装ではCopilot SDKのみを実装し、他のプロバイダーは拡張ポイントを用意するにとどめる。

Ollamaについては、Copilot SDKのBYOK対応プロバイダー・API形式との互換性を事前検証する。直接接続できない場合は、別のAgentProvider実装を追加する。

---

# 8. ルール検索

## 8.1 検索方式

初期段階ではSQLite FTS5を利用する。

検索対象：

- ルール名
- 基本方針
- 適用条件
- 例外条件
- 判断基準
- タグ

## 8.2 検索処理

```text
User Question
      |
      v
Query Analysis
      |
      v
Keyword Extraction
      |
      v
SQLite FTS5 Search
      |
      v
Candidate Rules
      |
      v
Filter by Status / Category
      |
      v
Return Relevant Rules
      |
      v
Agent Evaluation
```

検索結果は関連度順で返す。

検索対象は原則としてactiveのルールとする。

pending_approvalのルールは正式な判断根拠として使用しない。

## 8.3 将来拡張

将来的に以下を追加できる設計とする。

- Embeddingによるベクトル検索
- セマンティック検索
- ハイブリッド検索
- ルール間の関連性管理

初期段階ではベクトルDBを導入しない。

---

# 9. Webアプリケーション機能

## 9.1 画面構成

サイドバー：

- チャット
- ルール一覧
- 承認待ち
- 判断履歴
- 設定

## 9.2 チャット画面

主要機能：

- メッセージ送受信
- ストリーミング表示
- 会話履歴
- 新規会話
- 会話タイトル管理
- 適用ルール表示
- 判断根拠表示
- ルール提案の承認導線

回答は以下の形式で表示する。

```text
結論

原則としてデザートを与えないことを推奨します。

判断理由

登録されている家庭内ルールに基づく判断です。

適用ルール

[parenting-002]
夕食を残した場合のデザート

[詳細を見る]

例外

体調不良の場合は例外となります。
```

## 9.3 ルール一覧画面

機能：

- カテゴリ別表示
- キーワード検索
- ステータスフィルター
- タグ検索
- 新規登録
- 編集
- 詳細表示

ルールはカード形式で表示する。

表示項目：

- タイトル
- カテゴリ
- ルールの種類
- ステータス
- 更新日時

## 9.4 ルール詳細画面

以下を表示する。

- ルール全文
- 適用対象
- 適用条件
- 例外条件
- 判断基準
- 現在のバージョン
- 更新履歴
- 関連ルール

## 9.5 承認待ち画面

AIが提案したルールを一覧表示する。

各候補について以下の操作を提供する。

- 承認
- 修正して承認
- 却下
- 詳細確認

承認操作の前に、既存ルールとの重複・矛盾情報を表示する。

## 9.6 判断履歴画面

過去のAI回答を確認できるようにする。

保存項目：

- 質問内容
- AIの回答
- 会話日時
- 適用したルールID
- 適用したルールのバージョン
- 使用したプロバイダー
- 使用モデル
- エラー情報

重要なのは、判断時点のルールバージョンを記録することである。

後からルールが変更されても、過去の判断根拠を再現できるようにする。

---

# 10. API設計

FastAPIで以下のAPIを実装する。

## 10.1 Chat API

| Method | Endpoint | 説明 |
|---|---|---|
| POST | /api/chat/sessions | 新規会話 |
| GET | /api/chat/sessions | 会話一覧 |
| GET | /api/chat/sessions/{id} | 会話詳細 |
| POST | /api/chat/sessions/{id}/messages | メッセージ送信 |
| DELETE | /api/chat/sessions/{id} | 会話削除 |

メッセージ送信はSSEによるストリーミングを基本とする。

イベント例：

```text
event: message_start
data: {}

event: content_delta
data: {"text":"登録されているルールでは"}

event: rule_reference
data: {"rule_id":"parenting-001","version":1}

event: message_complete
data: {"message_id":"msg-001"}
```

エラー時は専用イベントを送信する。

## 10.2 Rule API

| Method | Endpoint | 説明 |
|---|---|---|
| GET | /api/rules | ルール一覧 |
| POST | /api/rules | ルール新規作成 |
| GET | /api/rules/{id} | ルール詳細 |
| PUT | /api/rules/{id} | ルール更新 |
| GET | /api/rules/{id}/versions | バージョン一覧 |
| POST | /api/rules/{id}/restore | バージョン復元 |
| GET | /api/rules/search | ルール検索 |

## 10.3 Approval API

| Method | Endpoint | 説明 |
|---|---|---|
| GET | /api/approvals | 承認待ち一覧 |
| GET | /api/approvals/{id} | 承認対象詳細 |
| POST | /api/approvals/{id}/approve | 承認 |
| POST | /api/approvals/{id}/reject | 却下 |

承認APIは認証された人間の操作のみ許可する。

## 10.4 History API

| Method | Endpoint | 説明 |
|---|---|---|
| GET | /api/history | 判断履歴一覧 |
| GET | /api/history/{id} | 判断履歴詳細 |

---

# 11. データベース設計

SQLiteに以下のテーブルを作成する。

## 11.1 conversations

- id
- title
- copilot_session_id
- provider
- model
- created_at
- updated_at

## 11.2 messages

- id
- conversation_id
- role
- content
- created_at
- metadata_json

## 11.3 rules

- id
- title
- category
- status
- rule_type
- priority
- current_version
- file_path
- created_at
- updated_at

## 11.4 rule_versions

- id
- rule_id
- version
- content_hash
- change_reason
- created_by
- approved_by
- created_at

## 11.5 approvals

- id
- rule_id
- status
- proposed_by
- reviewed_by
- review_comment
- created_at
- reviewed_at

## 11.6 judgment_logs

- id
- conversation_id
- message_id
- applied_rules_json
- provider
- model
- created_at

## 11.7 audit_logs

- id
- actor
- action
- resource_type
- resource_id
- details_json
- created_at

---

# 12. セキュリティ・安全性

家庭内の個人情報を扱うため、以下を必須とする。

## 12.1 認証

家庭内ネットワーク限定であっても認証機能を設ける。

初期段階ではシンプルなユーザー認証を実装する。

ユーザー単位の権限を考慮し、将来的に管理者・一般利用者を分けられる設計にする。

## 12.2 ネットワーク

- 原則として家庭内ネットワークからのみアクセス可能にする。
- Backendを不用意に外部公開しない。
- Docker内部ネットワークで通信する。
- 外部公開する場合はHTTPSと認証を必須とする。

## 12.3 APIキー

- APIキーをGitに登録しない。
- `.env`をGit管理対象外とする。
- ログにAPIキーを出力しない。
- 環境変数またはSecret管理で認証情報を保持する。

## 12.4 AIによる操作制限

以下を必須とする。

- AIによるルールの直接承認は禁止。
- AIによるルールの直接削除は禁止。
- AIが作成したルールは必ず承認待ちにする。
- ツール引数はPydanticで検証する。
- すべての変更操作を監査ログに記録する。

## 12.5 プロンプトインジェクション対策

ルール本文や会話内容に含まれる命令を、Agentのシステム指示として扱わない。

家庭内ルールは判断材料であり、AIのセキュリティ制約を上書きするものではない。

---

# 13. Docker構成

## 13.1 コンテナ構成

```text
docker-compose.yml

services:
  frontend:
    React Application

  backend:
    FastAPI
    Copilot SDK

volumes:
  household_data:
    /app/data
```

初期段階ではSQLiteを使用するため、独立したDBコンテナは不要。

## 13.2 永続化

以下を永続化する。

- SQLiteデータベース
- Markdownルール
- 会話履歴
- 設定情報

データはホスト側のディレクトリに保存する。

## 13.3 Copilot CLI

Copilot SDKの実行に必要なCLIランタイムをコンテナ内で利用できるようにする。

Python SDKが提供するCLI同梱方式を優先する。

認証状態をコンテナ再作成後も利用できるよう、認証情報の管理方法を検証する。

認証情報をコンテナイメージに含めてはならない。

---

# 14. テスト要件

## 14.1 ルール検索

- 登録済みルールを検索できる。
- 無関係なルールが過剰に返されない。
- archivedのルールを通常の判断対象にしない。
- pending_approvalを正式なルールとして扱わない。

## 14.2 AI判断

以下のシナリオをテストする。

### ケースA：ルールが存在する

質問：
「夕食を残した場合、デザートを与えてよい？」

期待結果：
登録済みルールを参照し、その内容に基づいて回答する。

### ケースB：ルールが存在しない

質問：
「休日の起床時間は何時にすべき？」

期待結果：
家庭内ルールが未登録であることを明示する。

### ケースC：例外条件に該当する

質問：
「体調不良で夕食を残した場合もデザートなし？」

期待結果：
登録済みの例外条件を考慮する。

### ケースD：ルールが矛盾する

期待結果：
矛盾するルールを両方提示し、勝手に一方を採用しない。

### ケースE：新しいルールの提案

質問：
「今後は夕食を残したらデザートなしにしよう。」

期待結果：
新しいルール候補を作成し、承認待ちとして保存する。

### ケースF：仮定の質問

質問：
「もし夕食を残したらデザートなしにしたらどうなる？」

期待結果：
仮定の質問を家庭内ルールとして登録しない。

## 14.3 承認処理

- 承認によってactiveになる。
- 却下によって有効ルールに反映されない。
- 承認履歴が残る。
- 既存ルールのバージョンが保持される。
- AIから承認APIを直接実行できない。

## 14.4 障害試験

- LLM接続失敗
- Copilot CLI起動失敗
- SQLite接続失敗
- Markdownファイル破損
- SSE切断
- セッション復元失敗

エラー発生時も、保存済みのルールや会話履歴が破損しないこと。

---

# 15. 開発フェーズ

以下の順番で実装する。

## Phase 0：技術検証

目的：Copilot SDKの動作確認。

実装内容：

1. Python環境構築
2. Copilot SDK導入
3. 認証確認
4. モデル指定
5. セッション作成
6. メッセージ送信
7. ストリーミング
8. Custom Tools実行
9. Skills読み込み
10. Docker内での動作確認

成果物：
最小限のCopilot SDKチャットアプリ。

完了条件：
Docker環境でチャットとCustom Toolsが動作する。

## Phase 1：ルール管理基盤

実装内容：

1. Markdownルールの読み書き
2. YAML Front Matter解析
3. Pydanticによる検証
4. SQLiteインデックス
5. FTS5検索
6. ルールのバージョン管理

成果物：
ルール管理API。

完了条件：
ルールを登録・検索・更新できる。

## Phase 2：AI Agent統合

実装内容：

1. Household Advisor Agent
2. household-advisor Skill
3. rule-management Skill
4. search_rules
5. get_rule
6. propose_rule
7. evaluate_rule
8. 判断根拠の構造化

成果物：
家庭内ルールを参照して回答するAI。

完了条件：
登録済みルールを参照し、ルールIDとともに回答できる。

## Phase 3：Web UI

実装内容：

1. Reactアプリケーション
2. チャット画面
3. ストリーミング表示
4. ルール一覧
5. ルール詳細
6. ルール編集
7. 承認画面

成果物：
基本的なWebアプリケーション。

完了条件：
ブラウザからすべての基本機能を利用できる。

## Phase 4：履歴・監査

実装内容：

1. 会話履歴
2. 判断履歴
3. ルール適用履歴
4. バージョン履歴
5. 監査ログ
6. セッション復元

成果物：
継続利用可能な家庭内AI。

## Phase 5：品質・運用改善

実装内容：

1. エラー処理
2. 認証
3. バックアップ
4. データ整合性検証
5. Docker運用改善
6. テスト拡充

成果物：
家庭内サーバーで常時運用できるアプリケーション。

---

# 16. 将来拡張

初期実装では不要だが、以下の拡張を妨げない設計にする。

## 16.1 MCP対応

家庭内ルール管理機能をMCP Serverとして公開する。

これにより、他のAIクライアントからも同じルールを参照できるようにする。

将来的にMCP Apps対応ホストを用意し、ルール編集や承認画面をチャット内に表示することも検討する。

## 16.2 Rule Auditor

ルールの矛盾や重複を定期的に検査する。

検査項目：

- 内容が重複するルール
- 相互に矛盾するルール
- 適用条件が曖昧なルール
- 例外条件が不足しているルール
- 長期間更新されていないルール

検出結果は警告として表示し、自動変更は行わない。

## 16.3 家族メンバー別のルール

将来的に以下のような対象を管理できるようにする。

- 家族全員
- 父親
- 母親
- 子ども
- 特定の子ども

## 16.4 ルール間の関係性

ルール間の関係を管理する。

例：

- 親ルール
- 子ルール
- 例外ルール
- 関連ルール
- 優先ルール

## 16.5 AIモデルの切り替え

将来的に以下を検討する。

- GitHub Copilot
- OpenAI API
- Anthropic API
- Ollama
- その他OpenAI互換API

プロバイダーごとのツール実行・ストリーミング・セッション管理の差異を吸収する。

---

# 17. 非機能要件

| 項目 | 要件 |
|---|---|
| 稼働 | Docker Compose |
| OS | Linux |
| 利用端末 | PC・スマートフォンのブラウザ |
| 同時利用 | 初期は少人数 |
| DB | SQLite |
| データ保存 | 永続化必須 |
| UI | 日本語 |
| タイムゾーン | Asia/Tokyo |
| チャット | ストリーミング |
| 検索 | FTS5 |
| バックアップ | SQLiteとMarkdownを対象 |
| ログ | エラー・監査ログ |
| 認証 | 必須 |
| 外部公開 | 初期は不要 |

---

# 18. 開発時のLLMへの指示

以下を実装上の絶対ルールとする。

1. 仕様書に記載された機能を段階的に実装する。
2. 一度にすべてを実装しようとしない。
3. 各Phaseの完了条件を満たしてから次に進む。
4. GitHub Copilot SDKのAPIは公式ドキュメントを確認する。
5. 存在が確認できないAPIやメソッドを推測して実装しない。
6. SDKのバージョンを固定する。
7. 実装時に公式仕様との差異があれば記録する。
8. AIの回答とルールの正本を分離する。
9. ルールの承認処理をAIに委ねない。
10. 重要な変更には必ずテストを追加する。
11. Docker Composeで再現可能な開発環境を維持する。
12. 将来のLLM変更を考慮し、プロバイダー固有のコードを分離する。

## 実装の進め方

まずPhase 0のみを実装する。

Phase 0の完了後、動作確認とコードレビューを実施する。

問題がなければPhase 1へ進む。

各Phase終了時には以下を報告する。

- 実装した機能
- 作成・変更したファイル
- テスト結果
- 未解決の問題
- 次のPhaseへの課題

仕様に不明点がある場合は、勝手に重要な設計判断を行わず、質問する。

---

# 19. 最終的な完成イメージ

このアプリケーションの最終目標は、家庭内の意思決定を支える知識基盤を構築することである。

例えば、次のような利用を想定する。

### ケース1：既存ルールの確認

「子どもがお菓子を欲しがっているけど、夕食前にあげてもいい？」

AIが登録済みルールを検索し、該当ルールと例外を提示する。

### ケース2：新しいルールの登録

「今後は寝る前にYouTubeを見せないことにしよう。」

AIがルール候補を作成し、既存ルールとの整合性を確認して承認待ちにする。

### ケース3：家庭内ルールの矛盾

「以前は許可していたのに、今はダメなの？」

AIが過去のバージョンや変更履歴を確認し、ルールが変更された経緯を説明する。

### ケース4：家庭内ルールの棚卸し

「現在の家庭内ルールを整理して、矛盾や曖昧な点がないか確認して。」

AIがルール全体を調査し、問題点を一覧化する。

### ケース5：一般論との比較

「一般的にはどうなの？我が家のルールは妥当？」

家庭内ルールと一般的な知識を明確に分けて提示する。

---

# 20. 完成の定義

以下をすべて満たした時点で初期バージョン完成とする。

- [ ] Docker Composeで起動できる
- [ ] Copilot SDKによるチャットが動作する
- [ ] Skillsを読み込める
- [ ] 家庭内ルールをMarkdownで管理できる
- [ ] ルールを検索できる
- [ ] AIがルールを参照して回答できる
- [ ] 回答にルールIDとバージョンが表示される
- [ ] 会話からルール候補を作成できる
- [ ] 人間による承認・却下ができる
- [ ] Web UIからルールを編集できる
- [ ] ルールのバージョン履歴を確認できる
- [ ] 過去の判断履歴を確認できる
- [ ] AIによる無断のルール変更を防止できる
- [ ] データをバックアップ・復元できる
- [ ] 認証とアクセス制御が機能する

以上。