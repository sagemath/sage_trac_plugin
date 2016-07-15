# sage_trac plugin

This Trac plugin provides features unique to the trac.sagemath.org Trac
instance, to meet needs of the SageMath project. Although some of its
features are fairly general and may be useful for other Trac sites.

The majority of this plugin consists of features for integrating the Trac site
with a git repository, for which the
[GitTrac](https://trac.edgewall.org/wiki/TracGit) plugin was found, at some
point or another, to be insufficient or too slow.

In place of the Trac repository browser this assumes that the (singular) git
repository for the Trac site is hosted on a web-based repository browser
like [cgit](https://git.zx2c4.com/cgit/about/).  The plugin supplies
integration with Trac tickets by allowing either a commit to be associated
with a ticket (by SHA-1 hash) or a branch name in the repository.  If
a branch is associated with a ticket, new commits to that branch are also
shown in the ticket history (with help of a post-receive hook that is not
currently included with this plugin).  Further, the branch field in tickets
is rendered as a link displaying a merge diff of the branch with the current
develop ("master") branch.

Other features include management of SSH keys used for
authentication/authorization of access to the repository via SSH (using
[gitolite](http://gitolite.com/gitolite/index.html)), RPC methods for reading
from the repository, and built-bot integration.

The plugin is intended to work with the latest dev version of Trac
(currently v1.1.6) but some features are not currently in use on
trac.sagemath.org and have gone unmaintained for a while.


## Components

The `sage_trac` plugin currently consists of five main components:

* [SshKeysPlugin](#SshKeysPlugin)
* [TicketBranch](#TicketBranch)
* [TicketLog](#TicketLog)
* [BranchSearchModule](#BranchSearchModule)
* [BuildBotHook](#BuildBotHook) (broken)

### SshKeysPlugin

This plugin provides a User Preferences panel for managing their SSH keys
used to access the git repository (or repositories, in principle).  This
is intended for integration with git repositories configured to use
gitolite for authorization.

The UI is currently very bare-bones--users simply copy an SSH public
key signature into a line in a text entry box, one key per line.

SSH keys are stored under the user's Trac username in the gitolite-admin
repository.  The `keys` directory in the gitolite-admin repository is
subdivided into directories named `00`, `01`, ..., `N`, ..., `ff`.  This
is to support users with multiple keys.  Each user's Nth key goes into the
subdirectory `N` (which is zero-padded).  As such users may only store up
to 256 keys.

This plugin works by making its own clone of the gitolite-admin repository
which it keeps in the Trac environment (by default), and commits to and
pushes from whenever users add, remove, or update their SSH keys.

#### Configuration

To enable this component add the following to trac.ini under the
`[components]` section:

```
[components]
...
sage_trac.sshkeys.sshkeysplugin = enabled
sage_trac.sshkeys.userdatastore = enabled
```

The `UserDataStore` component is an associated component that must be enabled
as well.

In order for this component to work, one other `trac.ini` settings must be
specified, under the `[sage_trac]` section:

```
[sage_trac]
...
gitolite_host = <hostname>
```

where `<hostname>` is the IP address or hostname of the server hosting the
git repository over SSH, and may be `localhost` if it is the same server that
Trac is run on.

**Important:** Finally, the user under which the Trac server runs (e.g.
`www-data` when Trac is run in Apache in the typical configuration) *must* have
an SSH key with R/W access on the gitolite-admin repository hosted on the
server given by `gitolite_host`.

This documentation will assume the reader is already familiar with
administering gitolite, but for a refresher you can read the
[documentation for adding users to gitolite](http://gitolite.com/gitolite/basic-admin.html#users).
For Trac, generate an SSH public/private key pair, and store them
in the `.ssh` directory under the Trac user's home directory (such as
`/var/www` for the typical case of Apaache) and ensure that appropriate
permissions are set for that directory and for the keys.

Then copy the public key and give it an unambiguous name like
`_trac.pub` and add it to the `gitolite-admin` keys directory.  Then
edit the gitolite config to give R/W access to the gitolite-admin
repository to the `_trac` user.

##### Optional configuration

This component takes two optional `trac.ini` settings:

* `[sage_trac]/gitolite_user`--the user to log in as when connecting to
  the gitolite server (default: `git`)
* `[sage_trac]/gitolite_admin--the path to the local clone of the
  gitolite-admin repository that Trac will commit to and use to push changes
  upstream (default: `/path/to/trac/env/gitolite-admin`)

#### Caveats

The trickiest thing about this plugin is keeping the local copy of the
gitolite-admin repository in a consistent state.  Many bugs related to
this have been squashed and it works fine almost always.  However, there
is a possibility for a server process to be shut down uncleanly while
in the middle of a `git` command, which *can* leave the repository in an
inaccessible state.  When this happens the plugin will normally try to
re-clone the local repository, but if this continues to fail manual
intervention may be required by an administrator.  If for some reason
users are failing to update their SSH keys this is the first thing to check.
There should also be relevant error messages in the Trac log.

#### UserDataStore

This component provides the database table used to store SSH keys.  Originally
it was intended to do more, but in the future will probably be merged into
the functionality of `SshKeysPlugin`, as there's no reason for it to be
separate anymore.

This component *must* be enabled in order for `SshKeysPlugin` to work.


### TicketBranch


### TicketLog


### BranchSearchModule


### BuildBotHook

This module is intended to provide integration with a build bot build status
on the ticket page, but it is currently disabled on trac.sagemath.org and is
not certain to work.
