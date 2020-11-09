# -*- coding: utf-8 -*-

import random

from genshi.builder import tag
from genshi.core import TEXT
from genshi.filters import Transformer

from trac.core import implements, TracError
from trac.config import Option
from trac.web.api import ITemplateStreamFilter
from trac.web.chrome import add_stylesheet, ITemplateProvider

from .common import _signature_re

from . import git_merger

import pkg_resources
import pygit2

FILTER_DATE = Transformer('//div[@id="ticket"]/div[@class="date"]')
FILTER_ID = Transformer('//div[@id="ticket"]/h2/a[@class="trac-id"]')
FILTER_BRANCH = Transformer('//td[@headers="h_branch"]')
FILTER_BRANCH_TEXT = Transformer('//td[@headers="h_branch"]/text()')


class TicketBox(git_merger.GitMerger):
    """
    A Sage-specific plugin which customizes the ticket box in various
    ways:

    * Formats the ``branch`` field of a ticket and applies changes to the
    ``branch`` field to the git repository.

    * Adds colorized formatting to the ticket ID based on the ticket status.

    * Adds the patchbot status icons.
    """
    implements(ITemplateStreamFilter, ITemplateProvider)

    release_manager_signature = Option(
            'sage_trac', 'release_manager_signature',
            'Release Manager <release@sagemath.org>',
            doc='signature to use on commits (especially merges) made '
                'through action of the project release manager (default: '
                '"Release Manager <release@sagemath.org>)')

    patchbot_url = Option(
            'sage_trac', 'patchbot_url', '',
            'base URL of the Sage patchbot server from which to show '
            'the ticket build status')

    # Templates on which this filter should be applied
    _templates = set(['ticket_change.html', 'ticket_preview.html',
                      'ticket.html'])

    def __init__(self):
        super(TicketBox, self).__init__()

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

        ticket = data.get('ticket')

        if filename in self._templates and ticket:
            add_stylesheet(req, 'sage_trac/sage-ticket.css')
        else:
            return stream

        filters = [
            # Add additional color coding to the ticket ID
            FILTER_ID.attr('class', 'trac-id-{0}'.format(ticket['status'])),
        ]

        if self.patchbot_url:
            # Add the patchbot status icons if a patchbot URL was given
            nonce = hex(random.randint(0, 1 << 60))
            ticket_url = '{0}/ticket/{1}'.format(
                    self.patchbot_url.rstrip('/'), ticket.id)
            base_url = '{0}/base.svg?nonce={1}'.format(ticket_url, nonce)
            status_url = '{0}/status.svg?nonce={1}'.format(ticket_url, nonce)
            elem = tag.div(
                tag.a(
                    tag.img(src=base_url, border=0, height=32),
                    tag.img(src=status_url, border=0, height=32),
                    href=ticket_url),
                class_='date')

            filters.append(FILTER_DATE.after(elem))

        branch = ticket['branch']
        github_action_url = f"https://github.com/sagemath/sagetrac-mirror/actions?query=workflow%3ALint+branch%3{branch}"
        github_badge_url = f"https://github.com/sagemath/sagetrac-mirror/workflows/Lint/badge.svg?branch={branch}"
        github_elem = tag.div(
            tag.a(tag.img(src=github_badge_url, border=0), href=github_action_url),
            class_='date')
        filters.append(FILTER_DATE.after(github_elem))
            
        filters.extend(self._get_branch_filters(req, ticket))

        def apply_filters(filters):
            s = stream
            for filter in filters:
                s |= filter
            return s

        return apply_filters(filters)

    def _get_branch_filters(self, req, ticket):
        """
        Return a list of filters to apply to the branch field, if it is
        set.
        """

        branch = ticket['branch']
        base_branch = ticket['base_branch']
        filters = []

        if not branch:
            return filters

        def merge_link(url=None, class_='positive_review'):
            if url is None:
                return FILTER_BRANCH_TEXT.map(unicode.strip, TEXT).wrap(
                        tag.span(class_=class_))

            return FILTER_BRANCH_TEXT.map(unicode.strip, TEXT).wrap(
                    tag.a(class_=class_, href=url))

        def commits_link(url):
            return FILTER_BRANCH.append(tag.span(' ')).\
                    append(tag.a('(Commits)', href=url))

        def error_filters(error):
            return [FILTER_BRANCH.attr("class", "needs_work"),
                    FILTER_BRANCH.attr("title", error)]

        def error(error, filters=[]):
            return filters + error_filters(error)

        branch = branch.strip()

        if base_branch is not None:
            base_branch = base_branch.strip()
            if not base_branch:
                base_branch = None

        try:
            is_sha, branch = self.generic_lookup(branch)
            if is_sha:
                filters.append(
                        FILTER_BRANCH_TEXT.replace(branch.hex[:7]+' '))

            if base_branch:
                _, base_branch_commit = self.generic_lookup(base_branch)
            else:
                base_branch_commit = None
        except (KeyError, ValueError) as err:
            if err.message.find('Ambiguous') < 0:
                return error("branch does not exist")
            else:
                return error("sha1 hash is too ambiguous")

        ret = self.peek_merge(branch, base_branch=base_branch)
        _, log_url = self.get_merge_url(req, branch, ret,
                                        base=base_branch_commit)

        # For the merge-url just always pass through the git-merger frontend
        params = []
        if base_branch != self.master_branch:
            params.append(('base', base_branch))

        if branch:
            git_merger_url = req.abs_href('/git-merger/' + branch.hex, params)
        else:
            git_merger_url = None

        if log_url is not None:
            filters.append(commits_link(log_url))

        if ret == git_merger.GIT_UPTODATE:
            filters.append(merge_link(git_merger_url))
            filters.append(
                    FILTER_BRANCH.attr("title", "already merged"))
        else:
            if ret == git_merger.GIT_FAILED_MERGE:
                return error("trac's automerging failed", filters)
            elif git_merger_url is None:
                # Shortcut in case no git merge was generated
                return filters

            if ret == git_merger.GIT_FASTFORWARD:
                filters.append(merge_link(git_merger_url))
                filters.append(
                        FILTER_BRANCH.attr("title", "merges cleanly (fast forward)"))
            elif ret is not None:
                filters.append(merge_link(git_merger_url))
                filters.append(
                        FILTER_BRANCH.attr("title", "merges cleanly"))
            else:
                filters.append(merge_link(git_merger_url, 'needs_review'))
                filters.append(
                        FILTER_BRANCH.attr("title", "no merge preview yet "
                                                    "(click to generate)"))

        return filters

    # ITemplateProvider methods
    def get_templates_dirs(self):
        return []

    def get_htdocs_dirs(self):
        return [('sage_trac',
                 pkg_resources.resource_filename('sage_trac', 'htdocs'))]
