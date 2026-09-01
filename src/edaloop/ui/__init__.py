"""edaloop Web UI(Chainlit 薄适配层)。

分层纪律:核心链路(generate/loop/ingest/refine)零依赖本包;本包只有
session.py(纯逻辑,可测)+ app.py(Chainlit 展示层)。换前端只重写 app.py。
"""
