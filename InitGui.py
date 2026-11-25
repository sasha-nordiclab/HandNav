# HandNav: 3D view navigation by hand gestures (webcam). Adds one toggle button to the View toolbar.
try:
    import handnav_gui
    handnav_gui.setup()
except Exception as _e:  # never block FreeCAD startup
    import FreeCAD
    FreeCAD.Console.PrintError("HandNav: %s\n" % _e)
