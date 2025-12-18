import os
from quart import Quart

def file_mtime_filter(app: Quart):
    @app.template_filter('file_mtime')
    def file_mtime(path: str) -> int:
        full_path = os.path.join(app.root_path, path)
        if os.path.exists(full_path):
            return int(os.path.getmtime(full_path))
        return 0

    return file_mtime
