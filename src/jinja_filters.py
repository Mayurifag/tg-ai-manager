import os

from jinja2 import pass_context
from quart import Quart


def file_mtime_filter(app: Quart):
    @app.template_filter("file_mtime")
    @pass_context
    def file_mtime(_context, path: str) -> int:
        # Read the current mtime when rendering, even for literal paths.
        try:
            return int(os.path.getmtime(os.path.join(app.root_path, path)))
        except OSError:
            return 0

    return file_mtime
