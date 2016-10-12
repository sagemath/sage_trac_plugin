# -*- coding: utf-8 -*-

from genshi.builder import tag
from genshi.core import TEXT
from genshi.filters import Transformer

from trac.core import *
from trac.config import Option
from trac.web.api import ITemplateStreamFilter
from trac.web.chrome import add_stylesheet, ITemplateProvider

from .common import GitBase, _signature_re

from . import git_merger

import pkg_resources
import pygit2

FILTER = Transformer('//td[@headers="h_branch"]')
FILTER_TEXT = Transformer('//td[@headers="h_branch"]/text()')


def signature_eq(sig1, sig2):
    return sig1.name == sig2.name and sig1.email == sig2.email


class TicketBranch(git_merger.GitMerger):
    """
    A Sage specific plugin which formats the ``branch`` field of a ticket and
    applies changes to the ``branch`` field to the git repository.
    """
    implements(ITemplateStreamFilter, ITemplateProvider)

    release_manager_signature = Option(
            'sage_trac', 'release_manager_signature',
            'Release Manager <release@sagemath.org>',
            doc='signature to use on commits (especially merges) made '
                'through action of the project release manager (default: '
                '"Release Manager <release@sagemath.org>)')

    def __init__(self):
        super(TicketBranch, self).__init__()

        m = _signature_re.match(self.release_manager_signature)
        if not m:
            raise TracError(
                '[sage_trac]/release_manager_signature in trac.ini must be '
                'in the "Name <email@example.com>" format')

        self._release_signature = pygit2.Signature(m.group(1), m.group(2))

    def filter_stream(self, req, method, filename, stream, data):
        """
        Reformat the ``branch`` field of a ticket to show the history of the
        linked branch.
        """
        branch = data.get('ticket', {'branch': None})['branch']
        if filename != 'ticket.html' or not branch:
            return stream

        def positive_review(url=None):
            if url is None:
                return FILTER.attr("class", "positive_review")

            return FILTER_TEXT.map(unicode.strip, TEXT).wrap(
                    tag.a(class_="positive_review",
                          href=url))

        def commits(url):
            return FILTER.append(tag.span(' ')).\
                    append(tag.a('(Commits)', href=url))

        def error_filters(error):
            return (FILTER.attr("class", "needs_work"),
                    FILTER.attr("title", error))

        def apply_filters(filters):
            s = stream
            for filter in filters:
                s |= filter
            return s

        def error(error, filters=()):
            filters = tuple(filters)+error_filters(error)
            return apply_filters(filters)

        add_stylesheet(req, 'sage_trac/sage-ticket.css')

        branch = branch.strip()

        filters = []

        try:
            is_sha, branch = self.generic_lookup(branch)
            if is_sha:
                filters.append(
                        FILTER_TEXT.replace(branch.hex[:7]+' '))
        except (KeyError, ValueError) as err:
            if err.message.find('Ambiguous') < 0:
                return error("branch does not exist")
            else:
                return error("sha1 hash is too ambiguous")

        ret = self.get_merge(branch)

        if ret == git_merger.GIT_UPTODATE:
            base, merge = self.find_base_and_merge(branch)

            if base is not None:
                filters.append(commits(self.log_url(base, branch)))

            if merge is None:
                filters.append(positive_review())
            else:
                filters.append(positive_review(self.commit_url(merge)))

            filters.append(
                    FILTER.attr("title", "already merged"))
        else:
            filters.append(commits(self.log_url(self.master, branch)))

            if ret == git_merger.GIT_FAILED_MERGE:
                return error("trac's automerging failed", filters)
            elif ret == git_merger.GIT_FASTFORWARD:
                filters.append(positive_review(self.diff_url(self.master,
                                                             branch)))
            else:
                filters.append(positive_review(self.diff_url(ret)))
                filters.append(
                        FILTER.attr("title", "merges cleanly"))

        return apply_filters(filters)

    def find_base_and_merge(self, branch):
        walker = self._git.walk(self.master.oid,
                pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_REVERSE)
        walker.hide(branch.oid)
        for commit in walker:
            if (branch.oid in (p.oid for p in commit.parents) and
                    signature_eq(commit.author, self._release_signature)):
                base = None
                for p in commit.parents:
                    if p.oid == branch.oid:
                        pass
                    elif base is None:
                        base = p.oid
                    else:
                        base = self._git.merge_base(base, p.oid)
                if base is not None:
                    base = self._git.get(base)
                return base, commit
        return None, None

    # ITemplateProvider methods
    def get_templates_dirs(self):
        return []

    def get_htdocs_dirs(self):
        return [('sage_trac',
                 pkg_resources.resource_filename('sage_trac', 'htdocs'))]
