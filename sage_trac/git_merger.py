# -*- coding: utf-8 -*-

import shutil
import subprocess
import tempfile
import os.path

import pygit2

from .common import (GitBase, _signature_re, GenericTableProvider, run_git,
                     hexify)

from trac.core import implements, TracError
from trac.config import Option
from trac.db.schema import Table, Column
from trac.ticket.model import Ticket
from tracrpc.api import IXMLRPCHandler

GIT_SPECIAL_MERGES = ('GIT_FASTFORWARD', 'GIT_UPTODATE', 'GIT_FAILED_MERGE')
for _merge in GIT_SPECIAL_MERGES:
    globals()[_merge] = _merge


def signature_eq(sig1, sig2):
    return sig1.name == sig2.name and sig1.email == sig2.email


class GitMerger(GitBase, GenericTableProvider):
    implements(IXMLRPCHandler)

    trac_signature = Option(
            'sage_trac', 'trac_signature', 'trac <trac@sagemath.org>',
            doc='`Name <email@example.com>` format signature to use '
                'for commits made to the Git repository by the Trac '
                'plugin (default: trac <trac@sagemath.org>)')

    _schema = [
        Table('merge_store', key='target')[
            Column('target'),
            Column('base'),
            Column('tmp')
        ]
    ]

    _schema_version = 1

    def __init__(self):
        super(GitMerger, self).__init__()

        m = _signature_re.match(self.trac_signature)
        if not m:
            raise TracError(
                '[sage_trac]/trac_signature in trac.ini must be in the '
                '"Name <email@example.com>" format')

        self._signature = pygit2.Signature(m.group(1), m.group(2))

    def peek_merge(self, commit):
        """
        See if the given commit already has a cached merge result.
        """

        return self._get_cache(commit)

    def get_merge(self, commit):
        ret = self._get_cache(commit)
        if ret is None:
            try:
                ret = self._merge(commit)
            except pygit2.GitError:
                ret = GIT_FAILED_MERGE

            self._set_cache(commit, ret)
        return ret

    def _get_cache(self, commit):
        with self.env.db_query as query:
            cached = list(query("""
                SELECT base, tmp FROM "merge_store" WHERE target=%s
                """, (commit.hex,)))

            if not cached:
                return None

            base, tmp = cached[0]

            if base != self.master.hex:
                with self.env.db_transaction as tx:
                    tx("DELETE FROM merge_store WHERE target=%s",
                       (commit.hex,))
                return None

        if tmp in GIT_SPECIAL_MERGES:
            return tmp

        return self._git.get(tmp)

    def _set_cache(self, commit, tmp):
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('DELETE FROM "merge_store" WHERE target=%s', (commit.hex,))
            if tmp not in GIT_SPECIAL_MERGES:
                tmp = tmp.hex
            cursor.execute('INSERT INTO "merge_store" VALUES (%s, %s, %s)',
                    (commit.hex, self.master.hex, tmp))

    def _merge(self, commit):
        tmpdir = tempfile.mkdtemp()

        try:
            # libgit2/pygit2 are ridiculously slow when cloning local paths
            ret, out = run_git('clone', self.git_dir, tmpdir,
                               '--branch=%s' % self.master_branch)
            if ret != 0:
                raise TracError('Failure to create temporary git repository '
                                'clone for merge preview of %s: %s' %
                                (commit.hex, out))

            repo = pygit2.Repository(tmpdir)
            merge, _ = repo.merge_analysis(commit.oid)
            if merge & pygit2.GIT_MERGE_ANALYSIS_FASTFORWARD:
                ret = GIT_FASTFORWARD
            elif merge & pygit2.GIT_MERGE_ANALYSIS_UP_TO_DATE:
                ret = GIT_UPTODATE
            else:
                # non-trivial merge, so run merge algorithm
                repo.merge(commit.oid)

                # record the files that changed
                changed = set()
                for file, s in repo.status().items():
                    if s != pygit2.GIT_STATUS_INDEX_DELETED:
                        changed.add(file)
                    file = os.path.dirname(file)
                    while file:
                        changed.add(file)
                        file = os.path.dirname(file)

                # write the merged tree
                # this will error if the merge wasn't clean
                merge_tree = repo.index.write_tree()

                # write objects to main git repo
                def recursive_write(tree, path=''):
                    for obj in tree:
                        new_path = os.path.join(path, obj.name)
                        if new_path in changed:
                            obj = repo.get(obj.oid)
                            if isinstance(obj, pygit2.Tree):
                                recursive_write(obj, new_path)
                            elif obj is None:
                                # probably a subproject reference
                                continue
                            else:
                                self._git.write(pygit2.GIT_OBJ_BLOB, obj.read_raw())
                    return self._git.write(pygit2.GIT_OBJ_TREE, tree.read_raw())
                merge_tree = recursive_write(repo.get(merge_tree))

                ret = self._git.get(
                        self._git.create_commit(
                            None, # don't update any refs
                            self._signature, # author
                            self._signature, # committer
                            'Temporary merge of %s into %s'%(commit.hex, repo.head.get_object().hex), # merge message
                            merge_tree, # commit's tree
                            [repo.head.get_object().oid, commit.oid], # parents
                        ))
        finally:
            # If an error occurred in the git clone the tmpdir may no longer
            # exist
            if os.path.exists(tmpdir):
                shutil.rmtree(tmpdir)
        return ret

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

    def get_merge_url(self, req, branch, merge_result=None):
        """
        Return the appropriate URL for a merge preview (or lack thereof),
        along with a URL for the log of commits merged.
        """

        if merge_result is None:
            merge_result = self.peek_merge(branch)

        if merge_result == GIT_UPTODATE:
            base, merge = self.find_base_and_merge(branch)

            if merge is None:
                merge_url = None
            else:
                merge_url = req.abs_href('/git-merger/' +
                                         hexify(branch))

            if base is None:
                log_url = None
            else:
                log_url = self.log_url(base, branch)
        else:
            log_url = self.log_url(self.master, branch)

            if merge_result == GIT_FAILED_MERGE:
                merge_url = None
            elif merge_result == GIT_FASTFORWARD:
                merge_url = self.diff_url(self.master, branch)
            elif merge_result is not None:
                # Should be a SHA1 hash
                merge_url = self.diff_url(merge_result)
            else:
                merge_url = req.abs_href('/git-merger/' + hexify(branch))

        return merge_url, log_url

    def getMerge(self, req, ticketnum):
        ticket = Ticket(self.env, ticketnum)
        req.perm(ticket.resource).require('TICKET_VIEW')
        try:
            commit = self.generic_lookup(ticket['branch'].strip())[1]
        except (KeyError, ValueError):
            return ''
        merge = self.get_merge(commit)
        if merge in GIT_SPECIAL_MERGES:
            return merge
        return merge.hex

    # IXMLRPCHandler methods
    def xmlrpc_namespace(self):
        return 'merger'

    def xmlrpc_methods(self):
        yield (None, ((str, int),), self.getMerge)
