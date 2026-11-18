"""最小 fastapi 桩：仅供 fixtures/check_uniqueness_offline.py 在无依赖环境导入真实
backend/app.py 并直接调用端点函数。不实现任何 HTTP 能力，不是正式依赖。"""
class HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def Header(default=None, **kw):
    return default


def _deco(*a, **k):
    def wrap(f):
        return f
    return wrap


class FastAPI:
    def __init__(self, **kw):
        self.kw = kw

    def add_middleware(self, *a, **k):
        pass

    def on_event(self, *a, **k):
        return _deco(*a, **k)

    def get(self, *a, **k):
        return _deco(*a, **k)

    def post(self, *a, **k):
        return _deco(*a, **k)

    def mount(self, *a, **k):
        pass
