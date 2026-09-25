from pathlib import Path

from minitemplate.engine import render


class TemplateLoader:
    def __init__(self, directory):
        self.directory = Path(directory)

    def load(self, name):
        # Bug: no path safety
        return (self.directory / name).read_text(encoding='utf-8')

    def render(self, name, context):
        return render(self.load(name), context)
