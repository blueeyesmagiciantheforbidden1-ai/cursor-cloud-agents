from pathlib import Path

from minitemplate.engine import render as render_template


class TemplateLoader:
    def __init__(self, directory):
        self.directory = Path(directory)

    def load(self, name):
        if '..' in name or name.startswith('/') or name.startswith('\\'):
            raise ValueError('unsafe template name')
        path = self.directory / name
        return path.read_text(encoding='utf-8')

    def render(self, name, context):
        return render_template(self.load(name), context)
