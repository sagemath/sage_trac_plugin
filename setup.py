#!/usr/bin/env python2

from setuptools import setup, find_packages

setup(
        name='sage_trac',
        version='0.1',
        packages=find_packages(),
        zip_safe=True,
        package_data={
            'sage_trac': ['templates/prefs_ssh_keys.html']
            },
        entry_points={
            'trac.plugins': 'sage_trac = sage_trac'
            },
        )
