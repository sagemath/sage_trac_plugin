#!/usr/bin/env python2

from setuptools import setup, find_packages

setup(
        name='sage_trac',
        version='0.2',
        packages=find_packages(),
        zip_safe=True,
        package_data={
            'sage_trac': [
                'templates/prefs_ssh_keys.html',
                ],
            },
        entry_points={
            'trac.plugins': [
                'sage_trac = sage_trac',
                'sage_trac.buildbot_hook = sage_trac.buildbot_hook',
                'sage_trac.search_branch = sage_trac.search_branch',
                'sage_trac.sshkeys = sage_trac.sshkeys',
                'sage_trac.ticket_branch = sage_trac.ticket_branch',
                'sage_trac.ticket_log = sage_trac.ticket_log',
                ]
            },
        )
