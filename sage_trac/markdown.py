"""
A simple Wiki macro/processor providing GitHub-ish style markdown support
including source code highlighting.
"""

from __future__ import absolute_import

import markdown

from markdown.extensions.codehilite import CodeHiliteExtension

from trac.mimeview.pygments import PygmentsRenderer
from trac.web.chrome import add_stylesheet
from trac.wiki.macros import WikiMacroBase


class MarkdownMacro(WikiMacroBase):
    """Implements ``#!markdown`` wiki processor."""

    def expand_macro(self, formatter, name, content):
        codehilite = CodeHiliteExtension(css_class='code')
        extensions = [
            codehilite,
            'markdown.extensions.fenced_code',
            'markdown.extensions.nl2br',  # GitHub/Lab-like behavior
            'markdown.extensions.tables'
        ]

        if hasattr(formatter, 'req') and formatter.req:
            # Hack needed to ensure that the correct pygments stylesheet
            # is included in the page
            req = formatter.req
            default_style = PygmentsRenderer(self.env).default_style
            add_stylesheet(req, '/pygments/{}.css'.format(
                req.session.get('pygments_style', default_style)))

        return markdown.markdown(content, extensions=extensions)

    def get_macros(self):
        yield 'markdown'
