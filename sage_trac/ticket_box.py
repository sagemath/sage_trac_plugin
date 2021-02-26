# -*- coding: utf-8 -*-

import random

from genshi.builder import tag
from genshi.core import TEXT
from genshi.filters import Transformer

from trac.cache import cached
from trac.core import implements, TracError
from trac.config import Option, ConfigSection
from trac.web.api import ITemplateStreamFilter
from trac.web.chrome import add_stylesheet, ITemplateProvider

from .common import _signature_re

from . import git_merger

import pkg_resources
import pygit2

FILTER_PROPERTIES = Transformer(
    '//div[@id="ticket"]/table[@class="properties"]')
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

    github_url = Option(
            'sage_trac', 'github_url', '',
            'base URL of the Sage GitHub project from which to show '
            'the ticket build status')

    gitlab_url = Option(
            'sage_trac', 'gitlab_url', '',
            'base URL of the Sage GitLab project from which to show '
            'the ticket build status')

    status_badges_config = ConfigSection('sage_trac:status_badges',
            """
            Define custom status badges to display on tickets in this section.
            Each status badge has a <name> which simply acts as an identity
            key for the badge, and several options in the format::

                <name>.link_url = <url for anchor tag wrapping the badge>
                <name>.img_url = <url for the badge image>
                <name>.order = <relative order of the badge>
                <name>.height = <height attribute for the image>
                <name>.width = <width attribute for the image>
                <name>.margin_left = <CSS margin left of the badge>
                <name>.margin_left = <CSS margin right of the badge>

            The ``link_url`` and ``img_url`` accept format string templates
            with the following template variables supported:

            * ``{nonce}``: a random nonce which can be appended to the URL;
              this can be used to prevent the browser from caching an image
              for example (this is used e.g. for the patchbot status badges)

            * ``{ticket_<field>}``: for each ticket field there is a variable
              ``ticket_<field>`` providing its value; e.g. ``ticket_id`` gives
              the ID of the ticket the badge is displayed on.

            The margin options may be useful for grouping related badges
            together.
            """)

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

    @cached
    def status_badges(self):
        config = self.status_badges_config
        badges = {}
        int_opts = set(['order', 'height', 'width'])
        for option, value in config.options():
            name, opt = option.split('.', 1)
            badge = badges.setdefault(name, {'name': name})
            if opt in int_opts:
                value = config.getint(option)
            badge[opt] = value

        def sort_key(b):
            return ('order' not in b, b.get('order'), b['name'])

        return sorted(badges.values(), key=sort_key)

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


        format_vars = {
            'nonce': hex(random.randint(0, 1 << 60)),
            'ticket_id': ticket.id
        }

        for k, v in ticket.values.items():
            format_vars['ticket_' + k] = v

        badge_tags = []
        for status_badge in self.status_badges:
            link_url = status_badge['link_url'].format(**format_vars)
            img_url = status_badge['img_url'].format(**format_vars)
            anchor_attrs = {'href': link_url}
            for anchor_opt in ('margin_left', 'margin_right'):
                if anchor_opt in status_badge:
                    anchor_attrs.setdefault('style', '')
                    anchor_attrs['style'] += (
                        '{attr}: {val};'.format(
                            attr=anchor_opt.replace('_', '-'),
                            val=status_badge[anchor_opt]))
            img_attrs = {
                'src': img_url,
                'border': 0
            }
            for img_opt in ('width', 'height'):
                if img_opt in status_badge:
                    img_attrs[img_opt] = status_badge[img_opt]
            badge_tags.append(tag.a(tag.img(**img_attrs), **anchor_attrs))

        filters.append(FILTER_PROPERTIES.after(
            tag.div(tag.h3('Status badges', id='comment:status-badges'),
                    tag.div(*badge_tags), class_='badges description')))
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
            links = [u' (']
            if url is not None:
                links.append(tag.a('Commits', href=url))
                links.append(u', ')

            link_vars = {'master': self.master_branch, 'branch': branch}

            if self.github_url:
                github_url = (self.github_url.rstrip('/') +
                              "/compare/{master}...{branch}")
                links.append(tag.a('GitHub',
                                   href=github_url.format(**link_vars)))
                links.append(u', ')
            if self.gitlab_url:
                gitlab_url = (self.gitlab_url.rstrip('/') +
                              "/-/compare/{master}...{branch}")
                links.append(tag.a('GitLab',
                                   href=gitlab_url.format(**link_vars)))

            links.append(u')')

            return FILTER_BRANCH.append(tag.span(*links))

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
            is_sha, branch_commit = self.generic_lookup(branch)
            if is_sha:
                filters.append(
                        FILTER_BRANCH_TEXT.replace(branch_commit.hex[:7]+' '))

            if base_branch:
                _, base_branch_commit = self.generic_lookup(base_branch)
            else:
                base_branch_commit = None
        except (KeyError, ValueError) as err:
            if err.message.find('Ambiguous') < 0:
                return error("branch does not exist")
            else:
                return error("sha1 hash is too ambiguous")

        ret = self.peek_merge(branch_commit, base_branch=base_branch)
        _, log_url = self.get_merge_url(req, branch_commit, ret,
                                        base=base_branch_commit)

        # For the merge-url just always pass through the git-merger frontend
        params = []
        if base_branch != self.master_branch:
            params.append(('base', base_branch))

        if branch:
            git_merger_url = req.abs_href('/git-merger/' + branch_commit.hex, params)
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
