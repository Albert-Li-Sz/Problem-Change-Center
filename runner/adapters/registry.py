from __future__ import annotations

import package_adapters as implementation

from . import dmoj, fps, generic, hoj, hydro, icpc, probhub, qduoj, uoj
from .protocol import Adapter


ADAPTERS: dict[str, Adapter] = {
    "probhub": probhub.create_adapter(
        detector=implementation.detect_extracted,
        reader=implementation.read_probhub,
    ),
    "hydro": hydro.create_adapter(
        detector=implementation.detect_extracted,
        reader=implementation.read_hydro,
        validator=implementation._validate_hydro_target,
        writer=implementation.write_hydro,
    ),
    "icpc": icpc.create_adapter(
        detector=implementation.detect_extracted,
        reader=implementation.read_icpc,
        validator=implementation._validate_icpc_target,
        writer=implementation.write_icpc,
    ),
    "hoj": hoj.create_adapter(
        detector=implementation.detect_extracted,
        reader=implementation.read_hoj,
        validator=implementation._validate_hoj_target,
        writer=implementation.write_hoj,
    ),
    "fps": fps.create_adapter(
        detector=implementation.detect_extracted,
        reader=implementation.read_fps,
        validator=implementation._validate_fps_target,
        writer=implementation.write_fps,
    ),
    "qduoj": qduoj.create_adapter(
        detector=implementation.detect_extracted,
        reader=implementation.read_qduoj,
        validator=implementation._validate_qduoj_target,
        writer=implementation.write_qduoj,
    ),
    "uoj": uoj.create_adapter(
        detector=implementation.detect_extracted,
        reader=implementation.read_uoj,
        validator=implementation._validate_uoj_target,
        writer=implementation.write_uoj,
    ),
    "dmoj": dmoj.create_adapter(
        detector=implementation.detect_extracted,
        reader=implementation.read_dmoj,
        validator=implementation._validate_dmoj_target,
        writer=implementation.write_dmoj,
    ),
    "generic": generic.create_adapter(
        detector=implementation.detect_extracted,
        reader=implementation.read_generic,
    ),
}
