# -*- coding: utf-8 -*-

from trac.core import *
from trac.config import ListOption, IntOption
from trac.ticket.api import ITicketManipulator

from .common import GitBase

import pygit2

class TicketLog(GitBase):
    implements(ITicketManipulator)

    ignore_branches = ListOption('sage_trac', 'ignore_branches_in_log', [],
                                 doc='branches to ignore when displaying the '
                                     'commit log in tickets; includes the '
                                     'branch in [trac]/master_branch by '
                                     'default, as well as "master" if '
                                     '[trac]/master_branch is different')

    max_new_commits = IntOption('sage_trac', 'max_new_commits', 10,
                                doc='maximum number of commits to display '
                                    'in the ticket log when new commits are '
                                    'pushed to an associated branch; in '
                                    'particular this limit prevents '
                                    'display of huge lists of commits from '
                                    'botched merges (default: 10)')

    def _valid_commit(self, val):
        if not isinstance(val, basestring):
            return
        if len(val) != 40:
            return
        try:
            int(val, 16)
            return val.lower()
        except ValueError:
            return

    def log_table(self, new_commit, limit=float('inf'), ignore=[]):
        walker = self._git.walk(self._git[new_commit].oid,
                pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_TIME)

        for b in ignore:
            c = self._git.lookup_branch(b)
            if c is None:
                try:
                    c = self._git.get(b)
                except ValueError:
                    continue
            else:
                c = c.get_object()

            if c is not None:
                walker.hide(c.oid)

        table = []

        for commit in walker:
            if len(table) >= limit:
                break
            short_sha1 = commit.hex[:7]
            title = commit.message.splitlines()
            if title:
                title = title[0]
            else:
                title = u''
            table.append(
                    u'||[%s %s]||{{{%s}}}||'%(
                        self.commit_url(commit),
                        short_sha1,
                        title))
        return table

    # doesn't actually do anything, according to the api
    def prepare_ticket(self, req, ticket, fields, actions): pass

    # hack changes into validate_ticket, since api is currently silly
    def validate_ticket(self, req, ticket):
        branch = ticket['branch']
        old_commit = self._valid_commit(ticket['commit'])
        if branch:
            ticket['branch'] = branch = branch.strip()
            commit = self._git.lookup_branch(branch)
            if commit is None:
                commit = ticket['commit'] = u''
            else:
                commit = ticket['commit'] = unicode(commit.get_object().hex)
        else:
            commit = ticket['commit'] = u''

        if (req.args.get('preview') is None and
                req.args.get('id') is not None and
                commit and
                commit != old_commit):
            ignore = set(self.ignore_branches)
            ignore.add(self.master_branch)
            ignore.add('master')
            if old_commit is not None:
                ignore.add(old_commit)
            try:
                table = self.log_table(commit, limit=self.max_new_commits+1,
                                       ignore=ignore)
            except (pygit2.GitError, KeyError):
                return []
            if len(table) > self.max_new_commits:
                header = u'Last {0} new commits:'.format(self.max_new_commits)
                table = table[:self.max_new_commits]
            else:
                header = u'New commits:'
            if table:
                comment = req.args.get('comment', u'').splitlines()
                if comment:
                    comment.append(u'----')
                comment.append(header)
                comment.extend(reversed(table))
                req.args['comment'] = u'\n'.join(comment)

        return []
