# SPDX-License-Identifier: MIT
"""GUI entry point. Runs at every start of FreeCAD, so it only declares the workbench."""
import os

import FreeCAD as App
import FreeCADGui as Gui

ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "icons")


class HandNavWorkbench(Gui.Workbench):
    MenuText = "HandNav"
    ToolTip = "Navigate the 3D view with hand gestures seen by a webcam"
    Icon = os.path.join(ICONS, "HandNav.svg")

    def Initialize(self):
        from . import gui

        gui.register()
        self.appendToolbar("HandNav", gui.COMMANDS)
        self.appendMenu("HandNav", gui.COMMANDS)

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(HandNavWorkbench())

# Off unless the user asks for it in the HandNav settings.
if App.ParamGet("User parameter:BaseApp/Preferences/Mod/HandNav").GetBool("ShowInAllWorkbenches", False):
    from . import gui

    gui.register()
    gui.show_in_all_workbenches()
