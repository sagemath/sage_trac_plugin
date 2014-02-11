# -*- coding: utf-8 -*-

from trac.core import Component, TracError
import pygit2
import os

MASTER_BRANCH = u'develop'
MASTER_BRANCHES = {u'develop', u'master'}

GIT_BASE_URL        = 'http://git.sagemath.org/sage.git/'
GIT_COMMIT_URL      = GIT_BASE_URL + 'commit/?id={commit}'
GIT_DIFF_URL        = GIT_BASE_URL + 'diff/?id={commit}'
GIT_DIFF_RANGE_URL  = GIT_BASE_URL + 'diff/?id2={base}&id={branch}'
GIT_LOG_RANGE_URL   = GIT_BASE_URL + 'log/?h={branch}&qt=range&q={base}..{branch}'

WWW_DATA_HOME = '/home/www-data'
GITOLITE_KEYDIR = os.path.join(WWW_DATA_HOME, 'gitolite', 'keydir')
GITOLITE_UPDATE = os.path.join(WWW_DATA_HOME, 'bin', 'gitolite-update')

class GitBase(Component):

    def __init__(self, *args, **kwargs):
        Component.__init__(self, *args, **kwargs)
        self.git_dir = self.config.get("trac","repository_dir","")
        if not self.git_dir:
            raise TracError("[trac] repository_dir is not set in the config file")

    @property
    def _git(self):
        try:
            return self.__git
        except AttributeError:
            self.__git = pygit2.Repository(self.git_dir)
            return self.__git
