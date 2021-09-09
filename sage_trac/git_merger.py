# -*- coding: utf-8 -*-

import re
import shutil
import tempfile
import os.path

import pygit2

from .common import (GitBase, _signature_re, GenericTableProvider, run_git)

from trac.core import implements, TracError
from trac.config import Option
from trac.db.schema import Table, Column
from trac.ticket.model import Ticket
from trac.web import IRequestHandler
from trac.web.chrome import add_warning
from tracrpc.api import IXMLRPCHandler

GIT_SPECIAL_MERGES = ('GIT_FASTFORWARD', 'GIT_UPTODATE', 'GIT_FAILED_MERGE')
for _merge in GIT_SPECIAL_MERGES:
    globals()[_merge] = _merge


def signature_eq(sig1, sig2):
    return sig1.name == sig2.name and sig1.email == sig2.email


class GitMerger(GitBase, GenericTableProvider):
    implements(IXMLRPCHandler, IRequestHandler)

    trac_signature = Option(
            'sage_trac', 'trac_signature', 'trac <trac@sagemath.org>',
            doc='`Name <email@example.com>` format signature to use '
                'for commits made to the Git repository by the Trac '
                'plugin (default: trac <trac@sagemath.org>)')

    _schema = [
        Table('merge_store', key='target')[
            Column('base'),
            Column('target'),
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

    def peek_merge(self, commit, base_branch=None):
        """
        See if the given commit already has a cached merge result.
        """

        if not base_branch:
            base_branch = self.master_branch
            base = self.master
        else:
            base = self.generic_lookup(base_branch)[1]

        return self._get_cache(commit, base)

    def get_merge(self, commit, base_branch=None):
        if not base_branch:
            base_branch = self.master_branch
            base = self.master
        else:
            base = self.generic_lookup(base_branch)[1]

        ret = self._get_cache(commit, base)
        if ret is None:
            try:
                ret = self._merge(commit, base_branch)
            except pygit2.GitError:
                ret = GIT_FAILED_MERGE

            self._set_cache(commit, base, ret)
        return ret

    def _get_cache(self, commit, base=None):
        with self.env.db_query as query:
            cached = list(query("""
                SELECT base, tmp FROM "merge_store" WHERE target=%s
                """, (commit.hex,)))

            if not cached:
                return None

            if base is None:
                return None

            cached_base, cached_tmp = cached[0]

            if cached_tmp in GIT_SPECIAL_MERGES:
                cached_obj = cached_tmp
            else:
                # Perhaps the cached merge commit no longer exists (e.g.
                # because has no parents maybe it got purged during garbage
                # collection on the repo), so we check that it still exists in
                # the repo and if not we just invalidate the cache in this case
                # and generate a new merge
                cached_obj = self._git.get(cached_tmp)

            if cached_base != base.hex or cached_obj is None:
                with self.env.db_transaction as tx:
                    tx("DELETE FROM merge_store WHERE target=%s",
                       (commit.hex,))
                return None

        return cached_obj

    def _set_cache(self, commit, base, tmp):
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('DELETE FROM "merge_store" WHERE target=%s',
                           (commit.hex,))

        with self.env.db_transaction as db:
            if tmp not in GIT_SPECIAL_MERGES:
                tmp = tmp.hex
            cursor = db.cursor()
            cursor.execute('INSERT INTO "merge_store" VALUES (%s, %s, %s)',
                    (base.hex, commit.hex, tmp))

    def _merge(self, commit, base_branch):
        tmpdir = tempfile.mkdtemp()

        try:
            # libgit2/pygit2 are ridiculously slow when cloning local paths
            ret, out = run_git('clone', self.git_dir, tmpdir,
                               '--branch=%s' % base_branch)
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
                            None,  # don't update any refs
                            self._signature,  # author
                            self._signature,  # committer
                            'Temporary merge of %s into %s' % (commit.hex, repo.head.get_object().hex),  # merge message
                            merge_tree,  # commit's tree
                            [repo.head.get_object().oid, commit.oid],  # parents
                        ))
        finally:
            # If an error occurred in the git clone the tmpdir may no longer
            # exist
            if os.path.exists(tmpdir):
                shutil.rmtree(tmpdir)
        return ret

    def find_base_and_merge(self, branch, base=None):
        if base is None:
            base = self.master

        walker = self._git.walk(base.oid,
                pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_REVERSE)
        walker.hide(branch.oid)
        for commit in walker:
            if (branch.oid in (p.oid for p in commit.parents) and
                    signature_eq(commit.author, self._release_signature)):
                found_base = None
                for p in commit.parents:
                    if p.oid == branch.oid:
                        pass
                    elif found_base is None:
                        found_base = p.oid
                    else:
                        found_base = self._git.merge_base(found_base, p.oid)
                if found_base is not None:
                    found_base = self._git.get(base.oid)
                return found_base, commit
        return None, None

    def get_merge_url(self, req, branch, merge_result=None, base=None):
        """
        Return the appropriate URL for a merge preview (or lack thereof),
        along with a URL for the log of commits merged.
        """

        if base is None:
            base = self.master

        if merge_result is None:
            merge_result = self.peek_merge(branch)

        if merge_result == GIT_UPTODATE:
            log_base, merge = self.find_base_and_merge(branch, base=base)

            if merge is None:
                merge_url = self.commit_url(branch)
            else:
                merge_url = self.commit_url(merge)

            if log_base is None:
                log_url = self.log_url(branch)
            else:
                log_url = self.log_url(log_base, branch)
        else:
            log_url = self.log_url(base, branch)

            if merge_result == GIT_FAILED_MERGE:
                merge_url = None
            elif merge_result == GIT_FASTFORWARD:
                merge_url = self.diff_url(base, branch)
            elif merge_result is not None:
                # Should be a SHA1 hash
                merge_url = self.diff_url(merge_result)
            else:
                # ???
                merge_url = None

        return merge_url, log_url

    def getMerge(self, req, ticketnum):
        ticket = Ticket(self.env, ticketnum)
        req.perm(ticket.resource).require('TICKET_VIEW')
        try:
            commit = self.generic_lookup(ticket['branch'].strip())[1]
        except (KeyError, ValueError):
            return ''

        try:
            base_branch = ticket['base_branch'].strip()
        except KeyError:
            base_branch = None

        merge = self.get_merge(commit, base_branch=base_branch)
        if merge in GIT_SPECIAL_MERGES:
            return merge
        return merge.hex

    # IXMLRPCHandler methods
    def xmlrpc_namespace(self):
        return 'merger'

    def xmlrpc_methods(self):
        yield (None, ((str, int),), self.getMerge)

    # IRequestHandler methods
    def match_request(self, req):
        match = re.match(r'/git-merger/(.+)$', req.path_info)
        if match:
            req.args['commit'] = match.group(1)
            return True

    def process_request(self, req):
        # Generate the merge preview for the specified commit and
        # redirect either directly to the merge preview if successful, or
        # back to the previous page if unsuccessful
        referer = req.get_header('Referer')
        if not referer:
            referer = req.base_path

        if 'commit' not in req.args:
            raise TracError('No commit specified for merge preview')

        commit_hex = req.args['commit']

        try:
            commit = self._git.get(commit_hex)
        except ValueError:
            commit = None

        if not isinstance(commit, pygit2.Commit):
            raise TracError('%s is not the hash for a known commit in '
                            'the repository' % commit_hex)

        base_branch = req.args.get('base', '').strip()
        if base_branch:
            try:
                base = self.generic_lookup(base_branch)[1]
            except (KeyError, ValueError):
                raise TracError("'%s' is not the name of a known branch "
                                "in the repository" % base_branch)
        else:
            base_branch = base = None

        merge = self.get_merge(commit, base_branch=base_branch)

        if merge == GIT_FAILED_MERGE:
            add_warning(req, 'Merge failed for %s' % commit_hex)
            req.redirect(referer)

        merge_url, _ = self.get_merge_url(req, commit, merge, base=base)

        if merge_url is None:
            # TODO: Maybe issue a notice about why this is happening
            # (I'm not even sure how this can happen??)
            req.redirect(referer)
        else:
            req.redirect(merge_url)
