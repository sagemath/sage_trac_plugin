0.3.3 (unreleased)
==================

* Nothing changed yet.


0.3.2 (2016-05-05)
==================

* Improved the SSH key plugin to use the Trac environment API for creating/
  upgrading the database table use by the plugin.

* Added validation for SSH keys entered by users--invalid keys will no longer
  be saved, either in the Trac database or in gitolite, and the user will be
  properly warned.


0.3.1 (2016-07-05)
==================

* Use ``recursive`` merge strategy with ``ours`` option instead of the plain
  ``ours`` merge strategy when pulling to the local clone of the
  gitolite-admin repository.  The use of ``-s ours`` was causing any updates
  to keys made by an administrator, outside of Trac, to be lost.


0.3 (2016-06-20)
================

* Initial version under new maintainer (@embray).
* Added a few new trac.ini options for values that were previously hard-coded
  in the source code (the defaults being mostly compatible with the original
  hard-coded values). [#13]
* Reworked handling of SSH keys.  The Trac plugin now creates and manages its
  own clone of the gitolite-admin repository (stored within the Trac
  environment by defaults) and commits, pulls, and pushes SSH key updates via
  that repository.  The only setup needed external to the plugin is to create
  an SSH key for the user that Trac runs under (e.g. www-data) and ensure that
  that SSH key is given R/W access to gitolite-admin on the server gitolite
  runs on. [#11]
* Links to cgit can now be accessed over HTTPS, and uses HTTPS by default.
  [#8]
