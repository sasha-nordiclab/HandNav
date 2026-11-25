# Changelog

## 0.11.1 (2026-11-25)

- Over the face, only a hand at the same distance as the face is ignored, judged independently of the pose. Run the tracker's installer again after the update.

## 0.11.0 (2026-11-25)

- A hand that touches the face is ignored. Run the tracker's installer again after the update.

## 0.10.0 (2026-11-25)

- Acceleration: fast movements go further, slow ones are finer. Run the tracker's installer again after the update; the panel shows the command.

## 0.9.1 (2026-11-25)

- The tracker's installer prepares the libraries, so the first start no longer takes up to a minute.

## 0.9.0 (2026-11-25)

- HandNav is a workbench of its own, in the namespaced `freecad/HandNav` layout. The button in every workbench is optional.
- The hand tracker is a separate program with its own installer and uninstaller; the addon installs and downloads nothing.
- Every packet between addon and tracker carries a secret, the stop signal included.
- Sensitivity sliders in the panel that act at once.
- Zoom by pushing or pulling the pinch, as with the fist.

## 0.7.0 (2026-11-25)

- Preference pages and a gesture guide in FreeCAD.
- Smallest hand size, sturdier tracking near the face.
- One-hand control, standard views by raised fingers, double squeeze to fit all.
