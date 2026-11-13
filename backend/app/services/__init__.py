"""apps 側サービス (会話 / 規則 / 判定 / 監視 / AG-UI 変換) を公開する。

§13 layering gate に従い、Provider SDK を直接 import するのは app/agent/ だけ。
このパッケージは app.agent の public 再 export だけが使う (§12.1 apps/agent 間)。
"""

from __future__ import annotations
