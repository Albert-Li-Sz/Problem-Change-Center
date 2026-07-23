from .factory import Detector, build_adapter
from .protocol import Adapter, Reader, TargetValidator, Writer


def create_adapter(
    *, detector: Detector, reader: Reader, validator: TargetValidator, writer: Writer
) -> Adapter:
    return build_adapter(
        "fps", detector=detector, reader=reader, validator=validator, writer=writer
    )
