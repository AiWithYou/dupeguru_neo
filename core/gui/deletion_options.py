# Created On: 2012-05-30
# Copyright 2015 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html

from hscommon.gui.base import GUIObject
from hscommon.trans import tr


class DeletionOptionsView:
    """Expected interface for :class:`DeletionOptions`'s view.

    *Not actually used in the code. For documentation purposes only.*

    The only file action offered here is recoverable quarantine. Permanent
    finalization is a separate operation outside this dialog.
    """

    def update_msg(self, msg: str):
        """Update the dialog's prompt with ``str``."""

    def show(self):
        """Show the dialog in a modal fashion.

        Returns whether the dialog was "accepted" (the user pressed OK).
        """


class DeletionOptions(GUIObject):
    """Confirm a recoverable quarantine action."""

    def __init__(self):
        GUIObject.__init__(self)

    def show(self, mark_count):
        """Prompt the user with a modal dialog offering our deletion options.

        :param int mark_count: Number of dupes marked for deletion.
        :rtype: bool
        :returns: Whether the user accepted the dialog (we cancel deletion if false).
        """
        msg = tr(
            "{} byte-verified file(s) will be moved to a recoverable quarantine. "
            "Every file and its keeper will be revalidated before anything moves. "
            "Permanent finalization is always a separate explicit operation."
        ).format(mark_count)
        self.view.update_msg(msg)
        return self.view.show()
