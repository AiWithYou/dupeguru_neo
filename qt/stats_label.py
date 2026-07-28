# Created By: Virgil Dupras
# Created On: 2010-02-12
# Copyright 2015 Hardcoded Software (http://www.hardcoded.net)
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file,
# which should be included with this package. The terms are also available at
# http://www.gnu.org/licenses/gpl-3.0.html


class StatsLabel:
    def __init__(self, model, view, on_refresh=None):
        self.view = view
        self.model = model
        self.on_refresh = on_refresh
        self.model.view = self

    def refresh(self):
        display = self.model.display
        self.view.setText(display)
        if self.on_refresh is not None:
            self.on_refresh(display)
