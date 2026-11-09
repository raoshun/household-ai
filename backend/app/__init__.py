"""家庭内ルール判断支援アプリのバックエンド。

`app.agent` だけが Copilot SDK を import する (§2.3)。それ以外の階層は
`app.agent` が公開するハンドルと正規化済みイベントだけを使う。
"""

from __future__ import annotations
