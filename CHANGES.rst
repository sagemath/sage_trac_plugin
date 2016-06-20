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
