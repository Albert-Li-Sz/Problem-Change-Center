from .factory import Detector, build_adapter
from .protocol import Adapter, Reader


def create_adapter(*, detector: Detector, reader: Reader) -> Adapter:
    return build_adapter("generic", detector=detector, reader=reader)
