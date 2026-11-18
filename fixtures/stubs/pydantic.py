"""最小 pydantic 桩：仅供离线核验脚本使用。"""
class BaseModel:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
