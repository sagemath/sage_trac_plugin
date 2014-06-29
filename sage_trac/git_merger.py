# -*- coding: utf-8 -*-

import subprocess
import os.path

from common import *

GIT_SPECIAL_MERGES = ('GIT_FASTFORWARD', 'GIT_UPTODATE', 'GIT_FAILED_MERGE')
for _merge in GIT_SPECIAL_MERGES:
    globals()[_merge] = _merge

TRAC_SIGNATURE = pygit2.Signature('trac', 'trac@sagemath.org')

class GitMerger(GitBase):

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
            if not cursor.rowcount:
                cursor.execute('CREATE TABLE "merge_store" ( base text, target text, tmp text, PRIMARY KEY ( target ), UNIQUE ( target, tmp ) )')

    def _drop_table(self):
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute('SELECT * FROM information_schema.tables WHERE "table_name"=%s', ('merge_store',))
            if cursor.rowcount:
                cursor.execute('DROP TABLE "merge_store"')

    def _merge(self, commit):
        import tempfile
        tmpdir = tempfile.mkdtemp()

        try:
            # libgit2/pygit2 are ridiculously slow when cloning local paths
            subprocess.call(['git', 'clone', self.git_dir, tmpdir, '--branch=%s'%MASTER_BRANCH])

            repo = pygit2.Repository(tmpdir)
            merge = repo.merge(commit.oid)
            if merge.is_fastforward:
                ret = GIT_FASTFORWARD
            elif merge.is_uptodate:
                ret = GIT_UPTODATE
            else:
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
                            else:
                                self._git.write(pygit2.GIT_OBJ_BLOB, obj.read_raw())
                    return self._git.write(pygit2.GIT_OBJ_TREE, tree.read_raw())
                merge_tree = recursive_write(repo.get(merge_tree))

                ret = self._git.get(
                        self._git.create_commit(
                            None, # don't update any refs
                            TRAC_SIGNATURE, # author
                            TRAC_SIGNATURE, # committer
                            'Temporary merge of %s into %s'%(commit.hex, repo.head.get_object().hex), # merge message
                            merge_tree, # commit's tree
                            [repo.head.get_object().oid, commit.oid], # parents
                        ))
        finally:
            import shutil
            shutil.rmtree(tmpdir)
        return ret
