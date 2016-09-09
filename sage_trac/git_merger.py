# -*- coding: utf-8 -*-

import subprocess
import os.path

import pygit2

from .common import GitBase, _signature_re

from trac.core import implements, TracError
from trac.config import Option
from trac.ticket.model import Ticket
from tracrpc.api import IXMLRPCHandler

GIT_SPECIAL_MERGES = ('GIT_FASTFORWARD', 'GIT_UPTODATE', 'GIT_FAILED_MERGE')
for _merge in GIT_SPECIAL_MERGES:
    globals()[_merge] = _merge


class GitMerger(GitBase):
    implements(IXMLRPCHandler)

    trac_signature = Option(
            'sage_trac', 'trac_signature', 'trac <trac@sagemath.org>',
            doc='`Name <email@example.com>` format signature to use '
                'for commits made to the Git repository by the Trac '
                'plugin (default: trac <trac@sagemath.org>)')

    def __init__(self):
        super(GitMerge, self).__init__()

        m = _signature_re.match(self.trac_signature)
        if not m:
            raise TracError(
                '[sage_trac]/trac_signature in trac.ini must be in the '
                '"Name <email@example.com>" format')

        self._signature = pygit2.Signature(m.group(1), m.group(2))

    def get_merge(self, commit):
        ret = GitMerger._get_cache(self, commit)
        if ret is None:
            try:
                ret = self._merge(commit)
            except pygit2.GitError:
                ret = GIT_FAILED_MERGE

            GitMerger._set_cache(self, commit, ret)
        return ret

    def _get_cache(self, commit):
        GitMerger._create_table(self)
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute('SELECT base, tmp FROM "merge_store" WHERE target=%s', (commit.hex,))
            try:
                base, tmp = cursor.next()
            except StopIteration:
                return None
        if base != self.master.hex:
            GitMerger._drop_table(self)
            return None
        if tmp in GIT_SPECIAL_MERGES:
            return tmp
        return self._git.get(tmp)

    def _set_cache(self, commit, tmp):
        GitMerger._create_table(self)
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('DELETE FROM "merge_store" WHERE target=%s', (commit.hex,))
            if tmp not in GIT_SPECIAL_MERGES:
                tmp = tmp.hex
            cursor.execute('INSERT INTO "merge_store" VALUES (%s, %s, %s)', (self.master.hex, commit.hex, tmp))

    def _create_table(self):
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('SELECT * FROM information_schema.tables WHERE "table_name"=%s', ('merge_store',))
            try:
                cursor.next()
            except StopIteration:
                cursor.execute('CREATE TABLE "merge_store" ( base text, target text, tmp text, PRIMARY KEY ( target ), UNIQUE ( target, tmp ) )')

    def _drop_table(self):
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('SELECT * FROM information_schema.tables WHERE "table_name"=%s', ('merge_store',))
            try:
                cursor.next()
                cursor.execute('DROP TABLE "merge_store"')
            except StopIteration:
                pass

    def _merge(self, commit):
        import tempfile
        tmpdir = tempfile.mkdtemp()

        try:
            # libgit2/pygit2 are ridiculously slow when cloning local paths
            subprocess.call(['git', 'clone', self.git_dir, tmpdir,
                '--branch=%s'% self.master_branch])

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
            import shutil
            shutil.rmtree(tmpdir)
        return ret

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
