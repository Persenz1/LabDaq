Windows uses its own signed WinUSB system driver. This package binds both USB identities.
The custom INF must be accompanied by a valid, trusted labdaq-winusb.cat for installation.
No catalog or signing credentials are currently supplied in this repository.
The installer must report this missing requirement and must not disable Windows signature enforcement.
Both hardware IDs are shared by DT9837A/B/C; the acquisition core checks subtype C.
