Folder Selection
================

The first window you see when you launch dupeGuru is the folder selection window. This windows
contains the basic input dupeGuru needs to start a scan:

* An Application Mode selection
* A Scan Type selection
* Folders to scan

Application Mode
----------------

dupeGuru had three main modes: Standard, Music and Picture.

Standard is for any type of files. This makes this mode the most polyvalent, but it lacks
specialized features other modes have.

Music mode scans only music files, but it supports tags comparison and its results window has many
audio-related informational columns.

Picture mode scans only pictures. **Visual similarity** is a perceptual matcher
that can find images that look alike without being the same file, while
**Byte-exact contents** uses the verified contents engine and preserves the
Picture review gallery.

Choosing an application mode not only changes available scan types in the selector below, but also
changes available options in the preferences panel. Thus, if you want to fine tune your scan, be
sure to open the preferences panel **after** you've selected the application mode.

Scan Type
---------

This selector determines the type of the scan we'll do. See :doc:`scan` for details about scan
types.

Folder List
-----------

To add a folder, click on the **+** button. If you added folder before, a popup
menu with a list of recent folders you added will pop. You can click on one of
them to add it directly to your list. If you click on the first item of the
popup menu, **Add New Folder...**, you will be prompted for a folder to add. If
you never added a folder, no menu will pop and you will directly be prompted
for a new folder to add.

An alternate way to add folders to the list is to drag them in the list.

To remove a folder, select the folder to remove and click on **-**. If a subfolder is selected when
you click the button, the selected folder will be set to **excluded** state (see below) instead of
being removed.

Folder states
-------------

Every folder can be in one of these four states:

**Incoming Files:**
    Files are normal comparison and review candidates. A file is eligible for
    quarantine only if the current scan proves it byte-exact and the live
    action checks pass.
**Protected Library:**
    Files participate in comparisons and receive keeper priority, but can never
    be destructive targets.
**Compare Only:**
    Files participate in comparisons and receive an immutable reference/keeper
    constraint so that they can never become destructive targets. Unlike a
    Protected Library item, that constraint does not claim the file is the
    authoritative or highest-quality original.
**Excluded:**
    Files in this directory are intentionally omitted from the scan.

The default state is **Incoming Files**. Use **Protected Library** for an
authoritative collection, and **Compare Only** for external media that should
inform matches but must not be changed.

When you set the state of a directory, all subfolders of this folder automatically inherit this
state unless you explicitly set a subfolder's state.

The advanced **Compare files only between different folder pools** option is
useful for an ingestion workflow. It suppresses groups contained entirely
inside one pool while retaining, for example, Incoming-to-Protected matches.

Scan
----

When you're ready, click on the **Scan** button to initiate the scanning process. When it's done,
you'll be shown the :doc:`results`.
